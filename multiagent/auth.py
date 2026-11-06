"""Username/password authentication for the REST API, WebSockets and web app.

Users live in a JSON file (`USERS_FILE`, default `users.json`) with salted PBKDF2 password
hashes. Logging in (`POST /auth/login`) returns a signed token, also set as an HttpOnly cookie
for the web app. Every API call then authenticates with one of:

    Cookie: session=<token>                  (web app, set by /auth/login)
    Authorization: Bearer <token>            (scripts, after /auth/login)
    Authorization: Basic <base64 user:pass>  (curl -u user:pass, no login step)
    ?token=<token>                           (WebSockets only, for clients that can't set headers)

When no users exist yet, an admin is created on startup from ADMIN_USERNAME / ADMIN_PASSWORD.
If ADMIN_PASSWORD isn't set, a random password is generated and printed to the server log once.

Env:
    USERS_FILE        default users.json
    ADMIN_USERNAME    default admin
    ADMIN_PASSWORD    password for the first admin (only used when no users exist)
    AUTH_TOKEN_HOURS  token lifetime, default 12
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection

log = logging.getLogger("multiagent.auth")

Role = Literal["admin", "user"]
USERNAME_PATTERN = r"^[A-Za-z0-9_.@-]{3,64}$"
MIN_PASSWORD = 8
COOKIE = "session"
PBKDF2_ITERATIONS = 240_000


class User(BaseModel):
    username: str
    role: Role = "user"
    created_at: datetime


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateUserRequest(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    password: str = Field(min_length=MIN_PASSWORD, max_length=256)
    role: Role = "user"


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=MIN_PASSWORD, max_length=256)


class SetPasswordRequest(BaseModel):
    password: str = Field(min_length=MIN_PASSWORD, max_length=256)


class TokenResponse(BaseModel):
    token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: User


# --- password hashing -------------------------------------------------------

def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
    except (ValueError, binascii.Error):
        return False
    return hmac.compare_digest(actual, expected)


# A real hash to check against for unknown usernames, so a login attempt takes the same
# time whether or not the user exists.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# --- store ------------------------------------------------------------------

class UserStore:
    """Users + the token-signing secret, in one JSON file (atomic writes, mode 0600)."""

    def __init__(self, path: str | Path | None = None, *, token_hours: float | None = None) -> None:
        self.path = Path(path) if path else None
        self.token_seconds = int((token_hours or float(os.getenv("AUTH_TOKEN_HOURS", "12"))) * 3600)
        self._lock = threading.RLock()
        self._data: dict = {"secret": secrets.token_urlsafe(48), "users": {}}
        if self.path and self.path.exists():
            self._data = json.loads(self.path.read_text())
        else:
            self._save()

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".users-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(self._data, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    @property
    def _users(self) -> dict[str, dict]:
        return self._data["users"]

    def _public(self, username: str) -> User:
        u = self._users[username]
        return User(username=username, role=u["role"], created_at=u["created_at"])

    # users
    def list(self) -> list[User]:
        with self._lock:
            return [self._public(n) for n in sorted(self._users)]

    def get(self, username: str) -> User | None:
        with self._lock:
            return self._public(username) if username in self._users else None

    def __len__(self) -> int:
        return len(self._users)

    def create(self, username: str, password: str, role: Role = "user") -> User:
        if not re.fullmatch(USERNAME_PATTERN, username):
            raise ValueError("Username must be 3-64 characters: letters, digits, _ . @ -")
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"Password must be at least {MIN_PASSWORD} characters")
        with self._lock:
            if username in self._users:
                raise ValueError(f"User '{username}' already exists")
            self._users[username] = {
                "password_hash": hash_password(password), "role": role, "token_version": 0,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            self._save()
            return self._public(username)

    def delete(self, username: str) -> None:
        with self._lock:
            if username not in self._users:
                raise KeyError(f"Unknown user '{username}'")
            admins = [n for n, u in self._users.items() if u["role"] == "admin"]
            if admins == [username]:
                raise ValueError("Can't delete the last admin")
            del self._users[username]
            self._save()

    def set_password(self, username: str, password: str) -> None:
        """Change a password. Also signs the user out everywhere (old tokens stop working)."""
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"Password must be at least {MIN_PASSWORD} characters")
        with self._lock:
            if username not in self._users:
                raise KeyError(f"Unknown user '{username}'")
            u = self._users[username]
            u["password_hash"] = hash_password(password)
            u["token_version"] += 1
            self._save()

    def authenticate(self, username: str, password: str) -> User | None:
        with self._lock:
            u = self._users.get(username)
        if u is None:
            verify_password(password, _DUMMY_HASH)
            return None
        return self._public(username) if verify_password(password, u["password_hash"]) else None

    # tokens:  base64(username|expiry|token_version) . base64(hmac-sha256)
    def issue_token(self, username: str) -> tuple[str, datetime]:
        with self._lock:
            version = self._users[username]["token_version"]
        exp = int(time.time()) + self.token_seconds
        payload = _b64(f"{username}|{exp}|{version}".encode())
        sig = _b64(hmac.new(self._data["secret"].encode(), payload.encode(), hashlib.sha256).digest())
        return f"{payload}.{sig}", datetime.fromtimestamp(exp, timezone.utc)

    def verify_token(self, token: str) -> User | None:
        try:
            payload, sig = token.split(".")
            expected = _b64(hmac.new(self._data["secret"].encode(), payload.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(sig, expected):
                return None
            username, exp, version = _unb64(payload).decode().rsplit("|", 2)
            if int(exp) < time.time():
                return None
        except (ValueError, binascii.Error, UnicodeDecodeError):
            return None
        with self._lock:
            u = self._users.get(username)
            if u is None or u["token_version"] != int(version):
                return None
        return self._public(username)

    def ensure_admin(self) -> None:
        """Create the first admin if there are no users yet."""
        with self._lock:
            if self._users:
                return
            username = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
            password = os.getenv("ADMIN_PASSWORD", "").strip()
            generated = not password
            if generated:
                password = secrets.token_urlsafe(12)
            self.create(username, password, role="admin")
        if generated:
            log.warning("Created admin user '%s' with generated password: %s  "
                        "(set ADMIN_PASSWORD in .env to choose it; change it after logging in)", username, password)
        else:
            log.warning("Created admin user '%s' from ADMIN_USERNAME / ADMIN_PASSWORD", username)


# --- request authentication ---------------------------------------------------

def _from_authorization(store: UserStore, header: str) -> User | None:
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer":
        return store.verify_token(value.strip())
    if scheme.lower() == "basic":
        try:
            username, _, password = base64.b64decode(value.strip()).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return None
        return store.authenticate(username, password)
    return None


def authenticate_connection(store: UserStore, conn: HTTPConnection, *, allow_query_token: bool = False) -> User | None:
    """The user making this HTTP request or WebSocket handshake, or None."""
    if header := conn.headers.get("authorization"):
        return _from_authorization(store, header)
    if allow_query_token and (token := conn.query_params.get("token")):
        return store.verify_token(token)
    if token := conn.cookies.get(COOKIE):
        # Browsers send cookies on cross-site WebSocket handshakes, so check the page's origin.
        origin = conn.headers.get("origin")
        if conn.scope["type"] == "websocket" and origin and origin.split("://", 1)[-1] != conn.headers.get("host"):
            return None
        return store.verify_token(token)
    return None


def unauthorized(detail: str = "Not authenticated. Log in, or send a Bearer token or Basic credentials.") -> HTTPException:
    # "Bearer" (not "Basic") so browsers don't pop up their own login dialog for the web app.
    return HTTPException(401, detail, headers={"WWW-Authenticate": "Bearer"})
