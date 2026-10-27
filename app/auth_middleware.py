"""Central authentication, tenant routing, and CSRF enforcement."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.auth import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, auth_repository
from app.config import get_config
from app.tenant import LEGACY_DATA_SCOPE, tenant_scope
from app.tenant_data import ensure_tenant_data


_PUBLIC_PATHS = {
    "/health",
    "/health/live",
    "/health/ready",
    "/login",
    "/api/auth/status",
    "/api/auth/login",
    "/vue.global.prod.js",
    "/marked.min.js",
    "/math-renderer.js",
    "/favicon.ico",
}
_PUBLIC_PATH_PREFIXES = ("/katex/",)
_LOGIN_RETURN_PATHS = {"/", "/docs", "/redoc"}
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _unauthorized(request: Request):
    if request.url.path.startswith("/api/") or request.url.path in {
        "/metrics", "/openapi.json"
    }:
        return JSONResponse(
            {
                "detail": "请先登录。",
                "code": "authentication_required",
            },
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    next_path = (
        request.url.path
        if request.url.path in _LOGIN_RETURN_PATHS
        else "/"
    )
    if next_path != "/" and request.url.query:
        next_path += f"?{request.url.query}"
    return RedirectResponse(
        f"/login?next={quote(next_path, safe='')}",
        status_code=303,
    )


def _security_headers(response, *, private: bool) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        # The current Vue global build compiles the in-page template at runtime
        # and therefore requires unsafe-eval until the frontend is prebuilt.
        "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'",
    )
    if private:
        response.headers["Cache-Control"] = "no-store"


async def authentication_middleware(request: Request, call_next):
    config = get_config().auth
    path = request.url.path
    if not config.enabled:
        with tenant_scope(LEGACY_DATA_SCOPE):
            request.state.auth_user = None
            response = await call_next(request)
        _security_headers(response, private=False)
        return response

    if (
        path in _PUBLIC_PATHS
        or path.startswith(_PUBLIC_PATH_PREFIXES)
        or request.method == "OPTIONS"
    ):
        with tenant_scope(LEGACY_DATA_SCOPE):
            response = await call_next(request)
        _security_headers(response, private=False)
        return response

    principal = None
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        principal = auth_repository.resolve_api_token(
            authorization.split(" ", 1)[1].strip()
        )
    if principal is None:
        principal = auth_repository.resolve_session(
            request.cookies.get(SESSION_COOKIE_NAME, "")
        )
    if principal is None:
        response = _unauthorized(request)
        _security_headers(response, private=True)
        return response

    request.state.auth_user = principal
    if principal["must_change_password"] and path not in {
        "/api/auth/me",
        "/api/auth/change-password",
        "/api/auth/logout",
    }:
        if path.startswith("/api/"):
            response = JSONResponse(
                {
                    "detail": "首次登录必须修改临时密码。",
                    "code": "password_change_required",
                },
                status_code=403,
            )
        else:
            response = RedirectResponse("/login?change=1", status_code=303)
        _security_headers(response, private=True)
        return response

    if (
        request.method in _UNSAFE_METHODS
        and principal["auth_method"] == "session"
    ):
        csrf_header = request.headers.get("x-csrf-token", "")
        csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME, "")
        csrf_valid = (
            bool(csrf_header)
            and hmac_compare(csrf_header, csrf_cookie)
            and auth_repository.verify_csrf(principal, csrf_header)
        )
        if not csrf_valid:
            response = JSONResponse(
                {
                    "detail": "CSRF 校验失败，请刷新页面后重试。",
                    "code": "csrf_failed",
                },
                status_code=403,
            )
            _security_headers(response, private=True)
            return response

    scope = principal["data_scope"]
    ensure_tenant_data(scope)
    with tenant_scope(scope):
        response = await call_next(request)
    if (
        request.method == "GET"
        and response.status_code == 404
        and not path.startswith("/api/")
        and "text/html" in request.headers.get("accept", "")
    ):
        # Recorded Markdown can contain absolute local source paths. Browsing
        # one of those links must not strand an authenticated user on the
        # static file handler's empty 404 page.
        response = RedirectResponse("/", status_code=303)
    _security_headers(response, private=True)
    return response


def hmac_compare(left: str, right: str) -> bool:
    """Constant-time compare kept local to avoid exposing token values."""
    import hmac

    return bool(left and right) and hmac.compare_digest(left, right)
