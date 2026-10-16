"""openclaw-gateway 入口。

路由结构（网关自有端点全部收在 /ocgw/ 前缀下，避免与 OpenClaw 原生路由冲突）：
- /ocgw/auth/login /ocgw/auth/callback /ocgw/auth/logout /ocgw/auth/me   CAS 认证
- /ocgw/healthz                                      网关自身健康检查
- 其余一切 HTTP / WebSocket                          透传到该用户的 OpenClaw Pod

免 token 自动进入：用户 Pod 的 OpenClaw 以 trusted-proxy 模式运行，网关注入
x-forwarded-user 等受信身份头，浏览器 CAS 登录后用原生设备身份直达 Control UI。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from app.auth import router as auth_router
from app.config import get_settings
from app.proxy import close_proxy_client, identity_headers, proxy_http
from app.sandbox_manager import get_sandbox_manager, init_sandbox_manager
from app.ws_proxy import proxy_websocket

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

_MANAGER_READY = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _MANAGER_READY
    settings = get_settings()
    init_sandbox_manager()
    await get_sandbox_manager().start_idle_gc()
    _MANAGER_READY = True
    logger.info(f"openclaw-gateway 就绪 mode={get_sandbox_manager().mode}")
    yield
    _MANAGER_READY = False
    manager = get_sandbox_manager()
    await manager.stop_idle_gc()
    manager.close()
    await close_proxy_client()


app = FastAPI(title="openclaw-gateway", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=get_settings().session_secret_key or "dev-insecure-session-key",
    same_site="lax",
    https_only=False,
)
app.include_router(auth_router, prefix="/ocgw")


# -------------------------------------------------------------- #
#  网关自有端点                                                    #
# -------------------------------------------------------------- #

@app.get("/ocgw/healthz")
async def gateway_healthz():
    if not _MANAGER_READY:
        return JSONResponse({"status": "starting"}, status_code=503)
    return {"status": "ok", "mode": get_sandbox_manager().mode}


# -------------------------------------------------------------- #
#  认证解析                                                       #
# -------------------------------------------------------------- #

def _resolve_user_http(request: Request) -> tuple[str | None, bool]:
    """HTTP 请求的用户解析。

    返回 (user_id, need_redirect)：user_id 为空且请求像浏览器导航时
    need_redirect=True，由调用方跳登录页；API 调用直接 401。
    """
    user_id = request.session.get("user_id")
    if user_id:
        return user_id, False
    settings = get_settings()
    if settings.auth_disabled:
        request.session["user_id"] = settings.default_dev_user_id
        request.session["username"] = settings.default_dev_user_id
        return settings.default_dev_user_id, False
    if "text/html" in request.headers.get("accept", ""):
        return None, True
    return None, False


# -------------------------------------------------------------- #
#  透传：HTTP                                                     #
# -------------------------------------------------------------- #

_PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


@app.api_route("/{path:path}", methods=_PROXY_METHODS)
async def catch_all_http(request: Request, path: str):
    user_id, need_redirect = _resolve_user_http(request)
    if not user_id:
        if need_redirect:
            return RedirectResponse(url="/ocgw/auth/login")
        raise HTTPException(status_code=401, detail="未登录")
    handle = await get_sandbox_manager().ensure_running(user_id)
    return await proxy_http(request, handle.base_url, f"/{path}")


# -------------------------------------------------------------- #
#  透传：WebSocket                                                #
# -------------------------------------------------------------- #

@app.websocket("/{path:path}")
async def catch_all_ws(ws: WebSocket, path: str):
    # SessionMiddleware 对 WS 握手同样解析 cookie（握手是 HTTP 请求，浏览器会带会话 cookie）
    user_id = None
    try:
        user_id = ws.session.get("user_id")
    except Exception:
        pass
    settings = get_settings()
    if not user_id and settings.auth_disabled:
        user_id = settings.default_dev_user_id
    if not user_id:
        await ws.accept()
        await ws.close(code=1008, reason="未登录")
        return

    manager = get_sandbox_manager()
    manager.retain(user_id)
    try:
        handle = await manager.ensure_running(user_id)
        # Origin 参与上游原生 origin 校验；身份头走 trusted-proxy 认证；
        # cookie/forwarded 头不得带给上游
        origin = ws.headers.get("origin")
        extra_headers = identity_headers(ws, user_id)
        if origin:
            extra_headers["Origin"] = origin
        await proxy_websocket(
            ws,
            handle.base_url,
            f"/{path}",
            user_id,
            extra_headers=extra_headers,
        )
    finally:
        manager.release(user_id)
