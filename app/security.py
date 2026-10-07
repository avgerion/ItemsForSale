from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 600_000
basic_auth = HTTPBasic(auto_error=False)


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
    if credentials is None:
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
