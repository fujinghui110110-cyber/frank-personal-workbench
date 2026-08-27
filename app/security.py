from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, status

from .config import Settings


STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
LOCALHOST_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})


ALL_SCOPES = frozenset({"*"})
WORKER_SCOPES = frozenset(
    {
        "intake:write",
        "jobs:claim",
        "jobs:write",
        "materials:read",
        "matters:read",
        "nodes:write",
        "wechat:read",
        "wechat:write",
    }
)
MCP_SCOPES = frozenset(
    {
        "intake:write",
        "jobs:claim",
        "jobs:write",
        "materials:read",
        "matters:read",
        "reminders:read",
        "work_packages:write",
        "nodes:write",
        "wechat:read",
        "wechat:write",
    }
)


@dataclass(frozen=True)
class AuthContext:
    actor: str
    role: str
    scopes: frozenset[str]

    def permits(self, scope: str) -> bool:
        return "*" in self.scopes or scope in self.scopes


def session_value(settings: Settings, lifetime_seconds: int = 60 * 60 * 24 * 30) -> str:
    expires = int(time.time()) + lifetime_seconds
    payload = f"owner|{expires}"
    signature = hmac.new(
        settings.session_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{payload}|{signature}"


def session_context(settings: Settings, value: str) -> AuthContext | None:
    try:
        actor, expires_text, signature = value.split("|", 2)
        expires = int(expires_text)
    except (ValueError, TypeError):
        return None
    if actor != "owner" or expires < int(time.time()):
        return None
    payload = f"{actor}|{expires}"
    expected = hmac.new(
        settings.session_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not secrets.compare_digest(signature, expected):
        return None
    return AuthContext("财务负责人", "owner", ALL_SCOPES)


def bearer_context(settings: Settings, token: str) -> AuthContext | None:
    candidates = (
        (settings.owner_token, AuthContext("财务负责人 API", "owner", ALL_SCOPES)),
        (settings.worker_token, AuthContext("Mac 执行节点", "worker", WORKER_SCOPES)),
        (settings.mcp_token, AuthContext("贾维斯 MCP", "mcp", MCP_SCOPES)),
    )
    for expected, context in candidates:
        if expected and secrets.compare_digest(token, expected):
            return context
    return None


def _is_allowed_browser_origin(request: Request, origin: str) -> bool:
    try:
        parsed = urlsplit(origin)
        parsed.port
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return False
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return False
    if parsed.hostname in LOCALHOST_NAMES:
        return True
    return origin.rstrip("/") == f"{request.url.scheme}://{request.url.netloc}".rstrip(
        "/"
    )


def enforce_browser_state_change_policy(request: Request) -> None:
    if request.method.upper() not in STATE_CHANGING_METHODS:
        return

    fetch_site = request.headers.get("sec-fetch-site", "")
    if any(value.strip().lower() == "cross-site" for value in fetch_site.split(",")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "禁止跨站状态变更请求")

    origin = request.headers.get("origin")
    if origin and not _is_allowed_browser_origin(request, origin):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "禁止跨站状态变更请求")


def auth_context(request: Request) -> AuthContext:
    settings: Settings = request.app.state.settings
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        context = bearer_context(settings, authorization[7:].strip())
        if context:
            return context
    enforce_browser_state_change_policy(request)

    cookie = request.cookies.get("workbench_session", "")
    context = session_context(settings, cookie)
    if context:
        return context
    if settings.password_disabled:
        return AuthContext("财务负责人", "owner", ALL_SCOPES)
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "请先登录")


def require_scope(scope: str) -> Callable[[AuthContext], AuthContext]:
    def dependency(context: AuthContext = Depends(auth_context)) -> AuthContext:
        if not context.permits(scope):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "当前凭据无权执行此操作")
        return context

    return dependency
