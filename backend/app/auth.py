"""Optional password protection for the whole app.

Set APP_PASSWORD in the environment and every /api and /ws request needs a valid
session cookie, obtained by POSTing the password to /api/auth/login. The static
frontend and /api/health stay public so the login page can load and container
health checks keep working. With APP_PASSWORD unset nothing changes.

The cookie is `<expiry>.<session id>.<HMAC-SHA256(expiry.session id)>`, signed with a key derived from
SECRET_KEY and the password, so changing either signs everyone out. Logging out also revokes that
session id on the server (kept in a small file, so it survives restarts), so a copied cookie stops working. It is
HttpOnly and SameSite=Lax, and Secure when the request came in over HTTPS
(directly or through a proxy that sets X-Forwarded-Proto).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from pathlib import Path
from http.cookies import SimpleCookie
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from app.config import settings

COOKIE_NAME = "insar_session"
SESSION_SECONDS = 30 * 24 * 3600

_PUBLIC_API = ("/api/health", "/api/auth/")
_MAX_FAILURES = 5
_LOCKOUT_SECONDS = 60


def enabled() -> bool:
    return bool(settings.app_password)


def _key() -> bytes:
    return hashlib.sha256(f"{settings.secret_key}\x00{settings.app_password}".encode()).digest()


def _sign(expiry: int, session_id: str) -> str:
    return hmac.new(_key(), f"{expiry}.{session_id}".encode(), hashlib.sha256).hexdigest()


# Revoked session ids and when each would have expired anyway (after that it can be forgotten).
_revoked: dict[str, int] = {}
_revoked_lock = threading.Lock()
_revoked_loaded = False


def _revoked_file() -> Path:
    return Path(settings.downloads_dir) / ".revoked_sessions.json"


def _load_revoked() -> None:
    global _revoked_loaded
    if _revoked_loaded:
        return
    _revoked_loaded = True
    try:
        _revoked.update({k: int(v) for k, v in json.loads(_revoked_file().read_text()).items()})
    except (OSError, ValueError, AttributeError):
        pass


def revoke(token: Optional[str]) -> None:
    """Stop accepting this session's cookie, whoever holds a copy of it."""
    parts = (token or "").split(".")
    if len(parts) != 3 or not parts[0].isdigit():
        return
    with _revoked_lock:
        _load_revoked()
        now = time.time()
        _revoked[parts[1]] = int(parts[0])
        for session_id in [k for k, expiry in _revoked.items() if expiry < now]:
            del _revoked[session_id]
        try:
            _revoked_file().write_text(json.dumps(_revoked))
        except OSError:
            pass                                      # still revoked for as long as this process lives


def make_token(now: Optional[float] = None) -> str:
    expiry = int((now if now is not None else time.time()) + SESSION_SECONDS)
    session_id = secrets.token_hex(8)
    return f"{expiry}.{session_id}.{_sign(expiry, session_id)}"


def valid_token(token: Optional[str], now: Optional[float] = None) -> bool:
    if not token or not enabled():
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    expiry_text, session_id, signature = parts
    try:
        expiry = int(expiry_text)
    except ValueError:
        return False
    if expiry < (now if now is not None else time.time()):
        return False
    if not hmac.compare_digest(signature, _sign(expiry, session_id)):
        return False
    with _revoked_lock:
        _load_revoked()
        return session_id not in _revoked


def password_matches(candidate: str) -> bool:
    return hmac.compare_digest(
        hashlib.sha256(candidate.encode()).digest(),
        hashlib.sha256(settings.app_password.encode()).digest(),
    )


def _is_protected(path: str) -> bool:
    if path.startswith(_PUBLIC_API):
        return False
    return path.startswith(("/api/", "/ws/")) or path in ("/api", "/ws")


def _cookie_from_headers(headers: list[tuple[bytes, bytes]]) -> Optional[str]:
    for name, value in headers:
        if name == b"cookie":
            jar: SimpleCookie = SimpleCookie()
            try:
                jar.load(value.decode("latin-1"))
            except Exception:
                return None
            morsel = jar.get(COOKIE_NAME)
            return morsel.value if morsel else None
    return None


class AuthMiddleware:
    """Pure ASGI middleware, so it guards WebSocket connections as well as HTTP."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or not enabled() or not _is_protected(scope["path"]):
            await self.app(scope, receive, send)
            return
        if valid_token(_cookie_from_headers(scope["headers"])):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return
        body = json.dumps({"detail": "Authentication required"}).encode()
        await send({
            "type": "http.response.start", "status": 401,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})


# ── Endpoints ────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/auth", tags=["auth"])

_failures: dict[str, list[float]] = {}   # recent failed logins, per client address
_MAX_TRACKED_CLIENTS = 1000


class LoginRequest(BaseModel):
    password: str


def _authenticated(request: Request) -> bool:
    return valid_token(request.cookies.get(COOKIE_NAME))


@router.get("/status")
def status(request: Request):
    return {"required": enabled(), "authenticated": (not enabled()) or _authenticated(request)}


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response):
    if not enabled():
        return {"required": False, "authenticated": True}

    # Locked out per client address, so someone guessing from elsewhere cannot lock the owner out.
    client = request.client.host if request.client else "unknown"
    now = time.time()
    for key in [k for k, v in _failures.items() if all(now - t >= _LOCKOUT_SECONDS for t in v)]:
        del _failures[key]
    recent = _failures.get(client, [])
    if len(recent) >= _MAX_FAILURES:
        raise HTTPException(429, "Too many failed attempts. Wait a minute and try again.")
    if not password_matches(body.password):
        if client in _failures or len(_failures) < _MAX_TRACKED_CLIENTS:
            _failures.setdefault(client, []).append(now)
        raise HTTPException(401, "Wrong password")

    _failures.pop(client, None)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        COOKIE_NAME, make_token(now), max_age=SESSION_SECONDS,
        httponly=True, samesite="lax", secure=secure, path="/",
    )
    return {"required": True, "authenticated": True}


@router.post("/logout")
def logout(request: Request, response: Response):
    revoke(request.cookies.get(COOKIE_NAME))
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"required": enabled(), "authenticated": False}
