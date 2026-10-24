from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.database import connect_db

PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 600_000
SESSION_COOKIE = "items_admin_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
basic_auth = HTTPBasic(auto_error=False)


class AdminLoginRequired(Exception):
    pass


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    salt_text = base64.urlsafe_b64encode(salt).decode("ascii").rstrip("=")
    digest_text = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"{PASSWORD_SCHEME}${PASSWORD_ITERATIONS}${salt_text}${digest_text}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iteration_text, salt_text, digest_text = encoded.split("$", 3)
        if scheme != PASSWORD_SCHEME:
            return False
        iterations = int(iteration_text)
        if not 100_000 <= iterations <= 2_000_000:
            return False
        salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
        expected = base64.urlsafe_b64decode(digest_text + "=" * (-len(digest_text) % 4))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def configured_users() -> dict[str, str]:
    raw_users = os.environ.get("INVENTORY_USERS", "")
    if not raw_users:
        return {}
    try:
        users = json.loads(raw_users)
    except json.JSONDecodeError as exc:
        raise RuntimeError("INVENTORY_USERS must be a JSON object of usernames to password hashes") from exc
    if not isinstance(users, dict) or any(
        not isinstance(username, str) or not isinstance(password_hash, str)
        for username, password_hash in users.items()
    ):
        raise RuntimeError("INVENTORY_USERS must map usernames to encoded password hashes")
    return users


def configured_agent_owners() -> dict[str, list[str]]:
    raw_access = os.environ.get("INVENTORY_AGENT_OWNERS", "")
    if not raw_access:
        return {}
    try:
        access = json.loads(raw_access)
    except json.JSONDecodeError as exc:
        raise RuntimeError("INVENTORY_AGENT_OWNERS must be a JSON object mapping agents to owner usernames") from exc
    if not isinstance(access, dict):
        raise RuntimeError("INVENTORY_AGENT_OWNERS must be a JSON object mapping agents to owner usernames")

    users = configured_users()
    validated: dict[str, list[str]] = {}
    for agent, owners in access.items():
        if (
            not isinstance(agent, str)
            or agent not in users
            or not isinstance(owners, list)
            or any(not isinstance(owner, str) or owner not in users or owner == agent for owner in owners)
            or len(set(owners)) != len(owners)
        ):
            raise RuntimeError("INVENTORY_AGENT_OWNERS contains an invalid agent or owner mapping")
        validated[agent] = owners
    return validated


def accessible_owners(username: str) -> list[str]:
    return list(dict.fromkeys([username, *configured_agent_owners().get(username, [])]))


def create_session_token(username: str) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = int(time.time()) + SESSION_TTL_SECONDS
    session_id_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    with connect_db() as connection:
        connection.execute(
            "INSERT INTO admin_sessions(session_id_hash, username, expires_at) VALUES (?, ?, ?)",
            (session_id_hash, username, expires_at),
        )
    return token


def session_username(token: str | None, users: dict[str, str]) -> str | None:
    if not token:
        return None
    try:
        session_id_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    except UnicodeEncodeError:
        return None
    with connect_db() as connection:
        row = connection.execute(
            "SELECT username FROM admin_sessions WHERE session_id_hash = ? AND expires_at > ?",
            (session_id_hash, int(time.time())),
        ).fetchone()
    if row is None or row["username"] not in users:
        return None
    return row["username"]


def revoke_session(token: str | None) -> None:
    if not token:
        return
    try:
        session_id_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    except UnicodeEncodeError:
        return
    with connect_db() as connection:
        connection.execute("DELETE FROM admin_sessions WHERE session_id_hash = ?", (session_id_hash,))


def is_same_origin_request(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return False
    parsed = urlsplit(origin)
    return parsed.scheme == request.url.scheme and parsed.netloc == request.url.netloc


async def require_admin(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials | None, Depends(basic_auth)],
) -> str:
    if os.environ.get("APP_ENV", "development").lower() == "production" and request.url.scheme != "https":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="HTTPS is required for administration")

    users = configured_users()
    if not users:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Administration is disabled until an administrator account is configured",
        )

    cookie_username = session_username(request.cookies.get(SESSION_COOKIE), users)
    if cookie_username:
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not is_same_origin_request(request):
            raise HTTPException(status_code=403, detail="A same-origin request is required")
        return cookie_username

    if credentials is None:
        if request.url.path == "/manage":
            raise AdminLoginRequired
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    encoded = users.get(credentials.username, "")
    valid = verify_password(credentials.password, encoded)
    if not valid:
        # Verify against a fixed dummy hash to keep unknown usernames on a comparable code path.
        verify_password(credentials.password, hash_password("invalid-user-placeholder", salt=b"items-for-sale"))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
