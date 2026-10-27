"""Browser login, account maintenance, and personal API-token endpoints."""

from __future__ import annotations

import secrets
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.auth import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    AuthError,
    auth_repository,
)
from app.config import get_config


router = APIRouter(prefix="/api/auth", tags=["auth"])


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(_StrictModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class ChangePasswordRequest(_StrictModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)


class CreateUserRequest(_StrictModel):
    username: str = Field(min_length=3, max_length=64)
    temporary_password: Optional[str] = Field(
        default=None, min_length=12, max_length=1024
    )
    is_admin: bool = False


class UpdateUserRequest(_StrictModel):
    is_active: bool


class CreateTokenRequest(_StrictModel):
    name: str = Field(min_length=1, max_length=100)
    expires_days: Optional[int] = Field(default=None, ge=1, le=3650)


def _safe_user(user: dict) -> dict:
    return {
        key: user.get(key)
        for key in (
            "id", "username", "is_admin", "is_active",
            "must_change_password", "last_login_at", "created_at", "updated_at",
        )
    }


def _principal(request: Request) -> dict:
    principal = getattr(request.state, "auth_user", None)
    if not principal:
        raise HTTPException(status_code=401, detail="请先登录。")
    return principal


def _admin(request: Request) -> dict:
    principal = _principal(request)
    if not principal["is_admin"]:
        raise HTTPException(status_code=403, detail="需要管理员权限。")
    return principal


def _map_error(exc: AuthError) -> HTTPException:
    status = 400
    if exc.code in {"invalid_credentials", "invalid_password"}:
        status = 401
    elif exc.code in {"login_locked", "login_busy"}:
        status = 429
    elif exc.code == "user_not_found":
        status = 404
    elif exc.code == "username_exists":
        status = 409
    headers = (
        {"Retry-After": str(exc.retry_after)}
        if exc.retry_after is not None
        else None
    )
    return HTTPException(
        status_code=status,
        detail=exc.detail,
        headers=headers,
    )


def _set_auth_cookies(
    response: JSONResponse,
    session_token: str,
    csrf_token: str,
) -> None:
    secure = bool(get_config().auth.cookie_secure)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        httponly=False,
        secure=secure,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"


def _clear_auth_cookies(response: JSONResponse) -> None:
    secure = bool(get_config().auth.cookie_secure)
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        secure=secure,
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        CSRF_COOKIE_NAME,
        path="/",
        secure=secure,
        httponly=False,
        samesite="strict",
    )


@router.get("/status")
def auth_status():
    config = get_config().auth
    return {
        "enabled": bool(config.enabled),
        "initialized": auth_repository.user_count() > 0,
        "password_min_length": 12,
    }


@router.post("/login")
def login(payload: LoginRequest, request: Request):
    try:
        user = auth_repository.authenticate(payload.username, payload.password)
    except AuthError as exc:
        raise _map_error(exc) from exc
    session_token, csrf_token, expires_at = auth_repository.create_session(
        user["id"],
        session_hours=get_config().auth.session_hours,
        ip_address=request.client.host if request.client else "",
        user_agent=request.headers.get("user-agent", ""),
    )
    # Accounts created while the service is already running receive their own
    # worker lazily at first login; it remains bound to this user's data scope.
    from app.enrichment import enrichment_manager

    enrichment_manager.start_for_scope(user["data_scope"])
    response = JSONResponse({
        "authenticated": True,
        "user": _safe_user(user),
        "session_expires_at": expires_at,
    })
    _set_auth_cookies(response, session_token, csrf_token)
    return response


@router.get("/me")
def me(request: Request):
    if not get_config().auth.enabled:
        return {"enabled": False, "authenticated": False, "user": None}
    principal = _principal(request)
    return {
        "enabled": True,
        "authenticated": True,
        "user": _safe_user(principal),
        "auth_method": principal["auth_method"],
        "session_expires_at": principal.get("session_expires_at"),
    }


@router.post("/logout")
def logout(request: Request):
    principal = _principal(request)
    if principal["auth_method"] == "session":
        auth_repository.revoke_session_hash(principal.get("session_hash", ""))
    response = JSONResponse({"authenticated": False})
    _clear_auth_cookies(response)
    response.headers["Clear-Site-Data"] = '"cache", "cookies", "storage"'
    return response


@router.post("/change-password")
def change_password(payload: ChangePasswordRequest, request: Request):
    principal = _principal(request)
    if principal["auth_method"] != "session":
        raise HTTPException(status_code=400, detail="请使用浏览器会话修改密码。")
    try:
        auth_repository.change_password(
            principal["id"],
            payload.current_password,
            payload.new_password,
        )
        user = auth_repository.get_user(principal["id"])
        session_token, csrf_token, expires_at = auth_repository.create_session(
            user["id"],
            session_hours=get_config().auth.session_hours,
            ip_address=request.client.host if request.client else "",
            user_agent=request.headers.get("user-agent", ""),
        )
    except AuthError as exc:
        raise _map_error(exc) from exc
    response = JSONResponse({
        "changed": True,
        "user": _safe_user(user),
        "session_expires_at": expires_at,
    })
    _set_auth_cookies(response, session_token, csrf_token)
    return response


@router.get("/users")
def list_users(request: Request):
    _admin(request)
    return {"items": [_safe_user(user) for user in auth_repository.list_users()]}


@router.post("/users", status_code=201)
def create_user(payload: CreateUserRequest, request: Request):
    _admin(request)
    temporary_password = payload.temporary_password or secrets.token_urlsafe(18)
    try:
        user = auth_repository.create_user(
            payload.username,
            temporary_password,
            is_admin=payload.is_admin,
            must_change_password=True,
            use_legacy_data=False,
        )
    except AuthError as exc:
        raise _map_error(exc) from exc
    from app.enrichment import enrichment_manager

    enrichment_manager.start_for_scope(user["data_scope"])
    return {
        "user": _safe_user(user),
        "temporary_password": temporary_password,
        "message": "临时密码只在本次响应中显示。",
    }


@router.post("/users/{user_id}/reset-password")
def reset_user_password(user_id: str, request: Request):
    principal = _admin(request)
    if user_id == principal["id"]:
        raise HTTPException(
            status_code=400,
            detail="请使用“修改密码”功能更新当前账户密码。",
        )
    temporary_password = secrets.token_urlsafe(18)
    try:
        auth_repository.set_password(
            user_id,
            temporary_password,
            must_change_password=True,
        )
        user = auth_repository.get_user(user_id)
    except AuthError as exc:
        raise _map_error(exc) from exc
    return {
        "user": _safe_user(user),
        "temporary_password": temporary_password,
        "message": "临时密码只在本次响应中显示。",
    }


@router.patch("/users/{user_id}")
def update_user(user_id: str, payload: UpdateUserRequest, request: Request):
    principal = _admin(request)
    if user_id == principal["id"] and not payload.is_active:
        raise HTTPException(status_code=400, detail="不能停用当前登录账号。")
    try:
        user = auth_repository.set_active(user_id, payload.is_active)
    except AuthError as exc:
        raise _map_error(exc) from exc
    from app.enrichment import enrichment_manager

    if payload.is_active:
        enrichment_manager.start_for_scope(user["data_scope"])
    else:
        enrichment_manager.stop_for_scope(user["data_scope"])
    return {"user": _safe_user(user)}


@router.get("/tokens")
def list_tokens(request: Request):
    principal = _principal(request)
    return {"items": auth_repository.list_api_tokens(principal["id"])}


@router.post("/tokens", status_code=201)
def create_token(payload: CreateTokenRequest, request: Request):
    principal = _principal(request)
    try:
        token, metadata = auth_repository.create_api_token(
            principal["username"],
            payload.name,
            expires_days=payload.expires_days,
        )
    except AuthError as exc:
        raise _map_error(exc) from exc
    return {
        "token": token,
        "metadata": metadata,
        "message": "Bearer Token 只在本次响应中显示。",
    }


@router.delete("/tokens/{token_id}")
def revoke_token(token_id: str, request: Request):
    principal = _principal(request)
    if not auth_repository.revoke_api_token(principal["id"], token_id):
        raise HTTPException(status_code=404, detail="Token 不存在。")
    return {"revoked": True}
