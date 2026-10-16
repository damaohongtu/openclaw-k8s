"""WebSocket 反向代理 — 浏览器 WS ↔ 用户 OpenClaw Pod WS。

Control UI 与 OpenClaw 网关的原生交互走 WebSocket；网关做纯透传桥接：
- 子协议协商透传（浏览器请求的子协议交给上游，回传上游选中的那个）
- 查询串原样保留（原生 token 若在 query 上则自然生效）
- 任一侧断开 → 双侧关闭，并释放沙盒活跃租约
"""

from __future__ import annotations

import logging
from typing import Optional

from starlette.websockets import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


class _WsClosed(Exception):
    """内部信号：桥接结束。"""


async def _pump_browser_to_upstream(ws: WebSocket, upstream) -> None:
    while True:
        msg = await ws.receive()
        if msg["type"] == "websocket.disconnect":
            raise _WsClosed()
        if "text" in msg and msg["text"] is not None:
            await upstream.send(msg["text"])
        elif "bytes" in msg and msg["bytes"] is not None:
            await upstream.send(msg["bytes"])


async def _pump_upstream_to_browser(upstream, ws: WebSocket) -> None:
    async for message in upstream:
        if isinstance(message, str):
            await ws.send_text(message)
        else:
            await ws.send_bytes(message)


async def proxy_websocket(
    ws: WebSocket,
    upstream_base_url: str,
    upstream_path: str,
    user_id: str,
    *,
    extra_headers: Optional[dict[str, str]] = None,
) -> None:
    """桥接浏览器 WS 到上游 OpenClaw WS。调用方负责沙盒租约 retain/release。"""
    from websockets.asyncio.client import connect

    query = ws.url.query
    upstream_uri = f"{upstream_base_url.replace('http://', 'ws://')}{upstream_path}"
    if query:
        upstream_uri = f"{upstream_uri}?{query}"

    # 子协议在 ASGI scope 里（Starlette WebSocket 无 .subprotocols 属性）
    scope_subprotocols = ws.scope.get("subprotocols") or []
    requested_subprotocols = list(scope_subprotocols) if scope_subprotocols else None
    import asyncio

    try:
        async with connect(
            upstream_uri,
            subprotocols=requested_subprotocols,
            additional_headers=extra_headers,
            open_timeout=10,
            close_timeout=5,
            max_size=64 * 1024 * 1024,
        ) as upstream:
            await ws.accept(subprotocol=upstream.subprotocol)
            tasks = [
                asyncio.create_task(_pump_browser_to_upstream(ws, upstream)),
                asyncio.create_task(_pump_upstream_to_browser(upstream, ws)),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                if not task.cancelled():
                    task.result()  # 异常交给外层处理（_WsClosed/Disconnect 吞掉，其余记录）
    except _WsClosed:
        pass
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"WS 桥接异常 user={user_id} path={upstream_path}: {e}")
        try:
            await ws.close(code=1011)
        except Exception:
            pass
