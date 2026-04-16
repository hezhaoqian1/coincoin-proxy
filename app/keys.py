from datetime import datetime

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .finance_summary import ensure_finance_summary_initialized, increment_finance_summary
from .models import ApiKey, User
from .rate_limiter import rate_limiter
from .schemas import KeyActivateRequest, KeyActivateResponse
from .security import generate_api_key, generate_id, generate_referral_code, hash_key


router = APIRouter(prefix="/v1/keys", tags=["keys"])
logger = logging.getLogger("coincoin.keys")

ACTIVATE_RATE_LIMIT = 5  # per IP per minute


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/activate", response_model=KeyActivateResponse)
async def activate_key(
    payload: KeyActivateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    if not payload.username and not payload.external_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="username or external_id required")

    ip = _client_ip(request)
    if not await rate_limiter.allow(f"activate:{ip}", ACTIVATE_RATE_LIMIT):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="too many requests, try later")

    user = None
    try:
        if payload.username:
            result = await db.execute(select(User).where(User.username == payload.username))
            user = result.scalar_one_or_none()
        if not user and payload.external_id:
            result = await db.execute(select(User).where(User.external_id == payload.external_id))
            user = result.scalar_one_or_none()

        if not user:
            user = User(
                id=generate_id("u_"),
                username=payload.username,
                external_id=payload.external_id,
                status="active",
                token_used=0,
                balance=settings.default_balance,
                referral_code=generate_referral_code(),
            )
            db.add(user)
            await db.flush()
            await ensure_finance_summary_initialized(db, user.id, commit=False)
            if settings.default_balance > 0:
                await increment_finance_summary(db, user.id, bonus_cents=settings.default_balance)
        else:
            if user.status != "active":
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="user blocked")
            if payload.username and not user.username:
                user.username = payload.username
            if payload.external_id and not user.external_id:
                user.external_id = payload.external_id

        api_key_value = generate_api_key()
        key = ApiKey(
            id=generate_id("k_"),
            user_id=user.id,
            key_hash=hash_key(api_key_value),
            kind="api",
            status="active",
            last_used_at=None,
            created_at=datetime.utcnow(),
        )
        db.add(key)
        await db.commit()

        return KeyActivateResponse(user_id=user.id, api_key=api_key_value, status="active")
    except HTTPException:
        raise
    except Exception:
        logger.exception("activate_key failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="internal error")
