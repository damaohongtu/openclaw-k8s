"""反向代理 — 网关 → 每用户 OpenClaw Pod。

改编自 eido-ai backend/app/gateway/proxy.py：
- HTTP：流式透传（SSE 禁缓冲），保留原方法/头/体
- WebSocket：双向桥接（浏览器 WS ↔ 上游 OpenClaw WS）
- 受信身份注入：OpenClaw 以 trusted-proxy 模式运行（本网关在其 trustedProxies 内），
  代理负责剥离客户端伪造的 forwarded/openclaw 头并注入网关自己的
  x-forwarded-user / x-forwarded-proto / x-forwarded-for，浏览器免 token 直达
"""

from __future__ import annotations

import ipaddress
import logging
from typing import AsyncIterator, Optional

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.requests import HTTPConnection

logger = logging.getLogger(__name__)

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

_FORWARDED_HEADERS = {
    "forwarded",
    "x-real-ip",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-forwarded-port",
    "x-forwarded-server",
    "x-forwarded-scheme",
}

# OpenClaw 只信任来自受信代理的这些头（http-auth-utils resolveTrustedHttpOperatorScopes
# 等直接消费）。客户端带来的必须剥掉，否则等于让浏览器自己声明权限（提权漏洞）。
_OPENCLAW_TRUSTED_HEADERS = {
    "x-openclaw-scopes",
    "x-openclaw-agent-id",
    "x-openclaw-agent",
}


# RFC 5737 TEST-NET-1：表示"真实来源不可知的外部客户端"。
# OpenClaw 的代理归属检查从 x-forwarded-for 链里跳过 loopback 与受信代理条目，
# 全部跳过 = 不可归属 → 403。两类源必须替换：
# - loopback：Docker Desktop 端口转发把外部流量伪装成 127.0.0.1
# - 落在受信 CIDR 内：kind 节点 cbr0 网关（10.244.0.1）——真实客户端不可能是
#   受信代理，出现即说明是节点 SNAT 伪装
# 生产集群 ingress 保留真实源 IP，不会走到占位符分支。
_UNKNOWN_CLIENT_IP = "192.0.2.1"


def _proxy_client_ip(conn: HTTPConnection) -> str:
    from app.sandbox_k8s import resolve_trusted_proxy_cidrs

    host = conn.client.host if conn.client else ""
    try:
        addr = ipaddress.ip_address(host.split("%", 1)[0])
        if addr.version == 6 and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        trusted = any(
            addr in ipaddress.ip_network(cidr, strict=False)
            for cidr in resolve_trusted_proxy_cidrs()
        )
        if not addr.is_loopback and not trusted:
            return host
    except ValueError:
        pass
    return _UNKNOWN_CLIENT_IP


def identity_headers(conn: HTTPConnection, user_id: str) -> dict[str, str]:
    """构造注入给上游 OpenClaw 的受信身份头（trusted-proxy 认证源）。

    HTTP 与 WS 共用（HTTPConnection 是 Request/WebSocket 的共同基类）：
    - x-forwarded-user：CAS 登录名（即 user_id，稳定标识；不用展示名）
    - x-forwarded-proto：客户端实际协议
    - x-forwarded-for：客户端地址（OpenClaw 要求非 loopback 才可归属）
    """
    return {
        "x-forwarded-user": user_id,
        "x-forwarded-proto": conn.url.scheme,
        "x-forwarded-for": _proxy_client_ip(conn),
    }


def get_proxy_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        timeout = httpx.Timeout(connect=5.0, read=None, write=60.0, pool=5.0)
        limits = httpx.Limits(max_connections=200, max_keepalive_connections=64)
        _client = httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=False, trust_env=False)
    return _client


async def close_proxy_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


_client: Optional[httpx.AsyncClient] = None


def _filter_request_headers(request: Request) -> dict[str, str]:
    # 统一小写 key：避免"浏览器小写原值 + 我们大写覆盖"在 dict 里共存，
    # 导致向上游发出重复头
    headers: dict[str, str] = {}
    for k, v in request.headers.items():
        lower = k.lower()
        if (
            lower in _HOP_BY_HOP
            or lower in _FORWARDED_HEADERS
            or lower in _OPENCLAW_TRUSTED_HEADERS
            or lower.startswith("x-forwarded-")
        ):
            continue
        headers[lower] = v
    return headers


def _filter_response_headers(resp: httpx.Response) -> dict[str, str]:
    return {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP}


async def _aiter_request_body(request: Request) -> AsyncIterator[bytes]:
    async for chunk in request.stream():
        if chunk:
            yield chunk


async def proxy_http(
    request: Request,
    base_url: str,
    upstream_path: str,
) -> Response:
    """通用 HTTP 反代：根据上游响应自动选择 streaming / buffered 模式。"""
    from app.sandbox_manager import get_sandbox_manager

    user_id = request.session.get("user_id", "")
    upstream_url = f"{base_url}{upstream_path}"
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    headers = _filter_request_headers(request)
    headers.update(identity_headers(request, user_id))
    # 压缩协商完全透传：浏览器请求什么编码就转发什么，上游压缩什么就原样回什么。
    # 之前强制 identity 因重复头 bug 失效过；透传后代理对压缩内容零处理。
    method = request.method.upper()

    client = get_proxy_client()
    manager = get_sandbox_manager()
    manager.retain(user_id)
    try:
        body_iter = None if method in ("GET", "HEAD", "DELETE") else _aiter_request_body(request)
        req = client.build_request(method=method, url=upstream_url, headers=headers, content=body_iter)
        upstream_resp = await client.send(req, stream=True)
    except httpx.ConnectError as e:
        manager.invalidate_health(user_id)
        manager.release(user_id)
        logger.warning(f"OpenClaw 沙盒不可达 {upstream_url}: {e}")
        raise HTTPException(status_code=502, detail=f"OpenClaw 沙盒不可达: {e}")
    except httpx.HTTPError as e:
        manager.release(user_id)
        logger.warning(f"代理失败 {upstream_url}: {e}")
        raise HTTPException(status_code=502, detail=f"代理失败: {e}")
    except BaseException:
        manager.release(user_id)
        raise

    response_headers = _filter_response_headers(upstream_resp)
    upstream_ct = upstream_resp.headers.get("content-type", "")
    is_event_stream = "text/event-stream" in request.headers.get("accept", "") or "text/event-stream" in upstream_ct
    is_file_response = "content-disposition" in upstream_resp.headers
    try:
        content_length = int(upstream_resp.headers.get("content-length", "0"))
    except ValueError:
        content_length = 0
    streaming = is_event_stream or is_file_response or content_length > 1024 * 1024

    if streaming:
        if is_event_stream:
            response_headers["Cache-Control"] = "no-cache, no-transform"
            response_headers["X-Accel-Buffering"] = "no"

        async def streaming_iter() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream_resp.aiter_raw():
                    if chunk:
                        yield chunk
            finally:
                await upstream_resp.aclose()
                manager.release(user_id)

        return StreamingResponse(
            streaming_iter(),
            status_code=upstream_resp.status_code,
            headers=response_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    try:
        # 原始字节透传（aread 会透明解压已知的 content-encoding，
        # 与透传的响应头不一致会让浏览器乱码）
        body = b"".join([chunk async for chunk in upstream_resp.aiter_raw()])
        return Response(content=body, status_code=upstream_resp.status_code, headers=response_headers)
    finally:
        await upstream_resp.aclose()
        manager.release(user_id)
