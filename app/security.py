import base64
import hashlib
import hmac as _hmac
import os
import secrets
from typing import Optional

from fastapi import HTTPException, Request, status

from .config import settings

PBKDF2_ITERATIONS = 260000


def generate_id(prefix: str) -> str:
    return f"{prefix}{secrets.token_hex(12)}"


def generate_api_key() -> str:
    raw = secrets.token_bytes(24)
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{settings.key_prefix}{token}"


def hash_key(key: str) -> str:
    payload = f"{settings.key_pepper}:{key}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    iterations = int(parts[1])
    salt = bytes.fromhex(parts[2])
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return _hmac.compare_digest(dk.hex(), parts[3])


def extract_api_key(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()

    for header_name in ("x-api-key", "api-key"):
        value = request.headers.get(header_name)
        if value:
            return value.strip()

    return None


def require_admin(request: Request) -> None:
    token = request.headers.get("x-admin-token") or request.headers.get("authorization")
    if token and token.lower().startswith("bearer "):
        token = token.split(" ", 1)[1].strip()
    if token != settings.admin_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
