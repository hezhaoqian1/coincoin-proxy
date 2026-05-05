from datetime import datetime

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .finance_summary import ensure_finance_summary_initialized, increment_finance_summary
from .models import ApiKey, User
from .rate_limiter import rate_limiter
from .proxy import authenticate_user
from .schemas import (
    DeveloperKeyCreateResponse,
    DeveloperKeyListItem,
    DeveloperKeyListResponse,
    DeveloperKeyStateResponse,
    DeveloperKeySummary,
    DeveloperKeyUpdateRequest,
    KeyActivateRequest,
    KeyActivateResponse,
)
from .security import encrypt_api_key, generate_api_key, generate_id, generate_referral_code, hash_key


router = APIRouter(prefix="/v1/keys", tags=["keys"])
logger = logging.getLogger("coincoin.keys")

ACTIVATE_RATE_LIMIT = 5  # per IP per minute


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _mask_api_key(raw_key: str) -> str:
    value = (raw_key or "").strip()
    if len(value) <= 12:
        return value
    return f"{value[:8]}...{value[-4:]}"


@router.get("/me", response_model=DeveloperKeyStateResponse)
async def get_my_developer_key_state(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await authenticate_user(request, db)

    active_count = (
        await db.execute(
            select(func.count())
            .select_from(ApiKey)
            .where(
                ApiKey.user_id == user.id,
                ApiKey.kind == "api",
                ApiKey.status == "active",
            )
        )
    ).scalar() or 0

    latest_key_row = (
        await db.execute(
            select(ApiKey)
            .where(
                ApiKey.user_id == user.id,
                ApiKey.kind == "api",
                ApiKey.status == "active",
            )
            .order_by(ApiKey.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    latest_key = None
    if latest_key_row:
        masked = ""
        if latest_key_row.encrypted_key:
            try:
                from .security import decrypt_api_key

                masked = _mask_api_key(decrypt_api_key(latest_key_row.encrypted_key) or "")
            except Exception:
                logger.warning("failed to decrypt developer key for summary", exc_info=True)
        latest_key = DeveloperKeySummary(
            key_id=latest_key_row.id,
            masked_key=masked or "sk_cc_...unknown",
            created_at=latest_key_row.created_at,
            last_used_at=latest_key_row.last_used_at,
            status=latest_key_row.status,
        )

    return DeveloperKeyStateResponse(
        has_active_key=active_count > 0,
        active_key_count=active_count,
        latest_key=latest_key,
    )


@router.get("", response_model=DeveloperKeyListResponse)
async def list_my_developer_keys(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await authenticate_user(request, db)
    rows = (
        await db.execute(
            select(ApiKey)
            .where(ApiKey.user_id == user.id, ApiKey.kind == "api")
            .order_by(ApiKey.created_at.desc())
            .limit(100)
        )
    ).scalars().all()

    items = []
    active = 0
    disabled = 0
    for row in rows:
        masked = "sk_cc_...unknown"
        raw_key = None
        if row.encrypted_key:
            try:
                from .security import decrypt_api_key

                raw_key = decrypt_api_key(row.encrypted_key) or ""
                masked = _mask_api_key(raw_key)
            except Exception:
                logger.warning("failed to decrypt developer key for list item", exc_info=True)
        if row.status == "active":
            active += 1
        elif row.status == "disabled":
            disabled += 1
        items.append(
            DeveloperKeyListItem(
                key_id=row.id,
                masked_key=masked,
                api_key=raw_key,
                status=row.status,
                created_at=row.created_at,
                last_used_at=row.last_used_at,
            )
        )

    return DeveloperKeyListResponse(
        total=len(items),
        active=active,
        disabled=disabled,
        data=items,
    )


@router.post("", response_model=DeveloperKeyCreateResponse)
async def create_my_developer_key(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await authenticate_user(request, db)

    api_key_value = generate_api_key()
    key = ApiKey(
        id=generate_id("k_"),
        user_id=user.id,
        key_hash=hash_key(api_key_value),
        encrypted_key=encrypt_api_key(api_key_value),
        kind="api",
        status="active",
        last_used_at=None,
        created_at=datetime.utcnow(),
    )
    db.add(key)
    await db.commit()

    return DeveloperKeyCreateResponse(
        key_id=key.id,
        api_key=api_key_value,
        masked_key=_mask_api_key(api_key_value),
        status=key.status,
        created_at=key.created_at,
    )


@router.patch("/{key_id}", response_model=DeveloperKeyListItem)
async def update_my_developer_key(
    key_id: str,
    payload: DeveloperKeyUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await authenticate_user(request, db)
    key = (
        await db.execute(
            select(ApiKey).where(
                ApiKey.id == key_id,
                ApiKey.user_id == user.id,
                ApiKey.kind == "api",
            )
        )
    ).scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="key not found")

    key.status = payload.status
    await db.commit()

    masked = "sk_cc_...unknown"
    raw_key = None
    if key.encrypted_key:
        try:
            from .security import decrypt_api_key

            raw_key = decrypt_api_key(key.encrypted_key) or ""
            masked = _mask_api_key(raw_key)
        except Exception:
            logger.warning("failed to decrypt developer key for update response", exc_info=True)

    return DeveloperKeyListItem(
        key_id=key.id,
        masked_key=masked,
        api_key=raw_key,
        status=key.status,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
    )


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
            encrypted_key=encrypt_api_key(api_key_value),
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
