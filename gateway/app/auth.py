"""CAS 认证端点：login / callback / logout / me（参考 eido-ai 同名实现）。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.config import get_settings

router = APIRouter()
logger = logging.getLogger(__name__)


def _cas_display_username(principal: str, attributes: object) -> str:
    if not isinstance(attributes, dict) or not attributes:
        return principal
    for key in ("cn", "displayName", "givenName", "name", "uid", "username"):
        raw = attributes.get(key)
        if raw is None:
            continue
        if isinstance(raw, bytes):
            s = raw.decode("utf-8", errors="replace").strip()
        else:
            s = str(raw).strip()
        if s:
            return s
    return principal


def _get_cas_client():
    from cas import CASClient  # type: ignore

    settings = get_settings()
    return CASClient(
        version=int(settings.cas_version),
        server_url=settings.cas_server_url_normalized,
        service_url=settings.cas_service_url,
    )


def _dev_login(request: Request) -> None:
    settings = get_settings()
    request.session["user_id"] = settings.default_dev_user_id
    request.session["username"] = settings.default_dev_user_id


@router.get("/auth/login")
async def login(request: Request):
    settings = get_settings()
    if settings.auth_disabled:
        _dev_login(request)
        return RedirectResponse(url=settings.frontend_url)
    if not settings.cas_server_url or not settings.cas_service_url:
        raise HTTPException(status_code=500, detail="CAS 未配置（CAS_SERVER_URL / CAS_SERVICE_URL）")
    return RedirectResponse(url=_get_cas_client().get_login_url())


@router.get("/auth/callback")
async def callback(request: Request, ticket: str = ""):
    settings = get_settings()
    if settings.auth_disabled:
        _dev_login(request)
        return RedirectResponse(url=settings.frontend_url)
    if not ticket:
        raise HTTPException(status_code=400, detail="缺少 ticket 参数")

    user, attributes, _pgtiou = _get_cas_client().verify_ticket(ticket)
    if not user:
        raise HTTPException(status_code=401, detail="CAS ticket 验证失败")

    request.session["user_id"] = user
    request.session["username"] = _cas_display_username(user, attributes)
    logger.info(f"CAS 登录成功: {user}")
    return RedirectResponse(url=settings.frontend_url)


@router.get("/auth/logout")
async def logout(request: Request):
    settings = get_settings()
    request.session.clear()
    if settings.auth_disabled:
        return RedirectResponse(url=settings.frontend_url)
    return RedirectResponse(url=_get_cas_client().get_logout_url(redirect_url=settings.frontend_url))


@router.get("/auth/me")
async def me(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")
    return {"user_id": user_id, "username": request.session.get("username", user_id)}
