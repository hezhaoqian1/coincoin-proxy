import base64
import hashlib
import secrets
from typing import Optional

from fastapi import HTTPException, Request, status

from .config import settings


def generate_id(prefix: str) -> str:
    return f"{prefix}{secrets.token_hex(12)}"


def generate_api_key() -> str:
    raw = secrets.token_bytes(24)
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{settings.key_prefix}{token}"


def hash_key(key: str) -> str:
    payload = f"{settings.key_pepper}:{key}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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
