from datetime import datetime, timezone
import ipaddress
import json

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .finance_summary import ensure_finance_summary_initialized, increment_finance_summary
from .models import ApiKey, RequestLog, User
from .rate_limiter import rate_limiter
from .proxy import authenticate_user
from .schemas import (
    DeveloperKeyCreateResponse,
    DeveloperKeyCreateRequest,
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


def _normalize_quota(value: int | None) -> int | None:
    if value is None:
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def _normalize_text(value: str | None, limit: int) -> str:
    return (value or "").strip()[:limit]


def _utc_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _normalize_ip_allowlist(values: list[str] | None) -> list[str]:
    if not values:
        return []
    normalized: list[str] = []
    seen = set()
    for raw in values:
        for chunk in str(raw or "").replace("\n", ",").split(","):
            item = chunk.strip()
            if not item:
                continue
            try:
                network = ipaddress.ip_network(item, strict=False)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"invalid ip allowlist entry: {item}",
                ) from exc
            text = str(network)
            if text not in seen:
                seen.add(text)
                normalized.append(text)
            if len(normalized) > 50:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="too many ip allowlist entries")
    return normalized


def _parse_ip_allowlist(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(item) for item in parsed if str(item).strip()]
    except Exception:
        pass
    return [item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()]


def _current_month_start() -> datetime:
    now = datetime.utcnow()
    return datetime(now.year, now.month, 1)


async def _load_key_usage(db: AsyncSession, key_ids: list[str]) -> dict[str, dict[str, int]]:
    if not key_ids:
        return {}
    month_start = _current_month_start()
    total_rows = (
        await db.execute(
            select(RequestLog.api_key_id, func.coalesce(func.sum(RequestLog.cost_cents), 0))
            .where(RequestLog.api_key_id.in_(key_ids))
            .group_by(RequestLog.api_key_id)
        )
    ).all()
    monthly_rows = (
        await db.execute(
            select(RequestLog.api_key_id, func.coalesce(func.sum(RequestLog.cost_cents), 0))
            .where(RequestLog.api_key_id.in_(key_ids), RequestLog.created_at >= month_start)
            .group_by(RequestLog.api_key_id)
        )
    ).all()
    usage = {key_id: {"total": 0, "monthly": 0} for key_id in key_ids}
    for key_id, cost in total_rows:
        if key_id in usage:
            usage[key_id]["total"] = int(cost or 0)
    for key_id, cost in monthly_rows:
        if key_id in usage:
            usage[key_id]["monthly"] = int(cost or 0)
    return usage


def _build_key_item(row: ApiKey, masked: str, raw_key: str | None, usage: dict[str, int] | None = None) -> DeveloperKeyListItem:
    usage = usage or {}
    return DeveloperKeyListItem(
        key_id=row.id,
        masked_key=masked,
        api_key=raw_key,
        name=getattr(row, "name", "") or "",
        purpose=getattr(row, "purpose", "") or "",
        status=row.status,
        expires_at=getattr(row, "expires_at", None),
        monthly_quota_cents=getattr(row, "monthly_quota_cents", None),
        total_quota_cents=getattr(row, "total_quota_cents", None),
        monthly_used_cents=int(usage.get("monthly", 0)),
        total_used_cents=int(usage.get("total", 0)),
        ip_allowlist=_parse_ip_allowlist(getattr(row, "ip_allowlist", None)),
        created_at=row.created_at,
        last_used_at=row.last_used_at,
    )


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
            name=getattr(latest_key_row, "name", "") or "",
            created_at=latest_key_row.created_at,
            last_used_at=latest_key_row.last_used_at,
            status=latest_key_row.status,
            expires_at=getattr(latest_key_row, "expires_at", None),
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

    usage_by_key = await _load_key_usage(db, [row.id for row in rows])
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
        items.append(_build_key_item(row, masked, raw_key, usage_by_key.get(row.id)))

    return DeveloperKeyListResponse(
        total=len(items),
        active=active,
        disabled=disabled,
        data=items,
    )


@router.post("", response_model=DeveloperKeyCreateResponse)
async def create_my_developer_key(
    request: Request,
    payload: DeveloperKeyCreateRequest | None = None,
    db: AsyncSession = Depends(get_db),
):
    user = await authenticate_user(request, db)
    payload = payload or DeveloperKeyCreateRequest()
    ip_allowlist = _normalize_ip_allowlist(payload.ip_allowlist)

    api_key_value = generate_api_key()
    key = ApiKey(
        id=generate_id("k_"),
        user_id=user.id,
        key_hash=hash_key(api_key_value),
        encrypted_key=encrypt_api_key(api_key_value),
        kind="api",
        name=_normalize_text(payload.name, 100),
        purpose=_normalize_text(payload.purpose, 255),
        status="active",
        expires_at=_utc_naive(payload.expires_at),
        monthly_quota_cents=_normalize_quota(payload.monthly_quota_cents),
        total_quota_cents=_normalize_quota(payload.total_quota_cents),
        ip_allowlist=json.dumps(ip_allowlist, ensure_ascii=True) if ip_allowlist else None,
        last_used_at=None,
        created_at=datetime.utcnow(),
    )
    db.add(key)
    await db.commit()

    return DeveloperKeyCreateResponse(
        key_id=key.id,
        api_key=api_key_value,
        masked_key=_mask_api_key(api_key_value),
        name=key.name,
        purpose=key.purpose,
        status=key.status,
        expires_at=key.expires_at,
        monthly_quota_cents=key.monthly_quota_cents,
        total_quota_cents=key.total_quota_cents,
        ip_allowlist=ip_allowlist,
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

    if payload.status is not None:
        key.status = payload.status
    if payload.name is not None:
        key.name = _normalize_text(payload.name, 100)
    if payload.purpose is not None:
        key.purpose = _normalize_text(payload.purpose, 255)
    if "expires_at" in payload.model_fields_set:
        key.expires_at = _utc_naive(payload.expires_at)
    if "monthly_quota_cents" in payload.model_fields_set:
        key.monthly_quota_cents = _normalize_quota(payload.monthly_quota_cents)
    if "total_quota_cents" in payload.model_fields_set:
        key.total_quota_cents = _normalize_quota(payload.total_quota_cents)
    if payload.ip_allowlist is not None:
        normalized_ip_allowlist = _normalize_ip_allowlist(payload.ip_allowlist)
        key.ip_allowlist = json.dumps(normalized_ip_allowlist, ensure_ascii=True) if normalized_ip_allowlist else None
    await db.commit()
    try:
        from .proxy import key_cache

        await key_cache.delete(key.key_hash)
    except Exception:
        logger.warning("failed to invalidate developer key auth cache", exc_info=True)

    masked = "sk_cc_...unknown"
    raw_key = None
    if key.encrypted_key:
        try:
            from .security import decrypt_api_key

            raw_key = decrypt_api_key(key.encrypted_key) or ""
            masked = _mask_api_key(raw_key)
        except Exception:
            logger.warning("failed to decrypt developer key for update response", exc_info=True)

    usage_by_key = await _load_key_usage(db, [key.id])
    return _build_key_item(key, masked, raw_key, usage_by_key.get(key.id))


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
