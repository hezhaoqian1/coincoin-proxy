import os
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .epay import EpayVerificationError, epay_configured, extract_epay_params_from_proof_url, verify_epay_callback_params
from .finance_summary import (
    build_user_finance_snapshot,
    build_user_finance_snapshots,
    ensure_finance_summary_initialized,
)
from .models import (
    Announcement,
    ApiKey,
    PaymentOrder,
    RechargeLog,
    RedemptionCode,
    ReferralReward,
    RequestLog,
    Station,
    StationCustomerLink,
    UsageDaily,
    User,
)
from .payment_common import PaymentConfirmError, confirm_paid_order
from .schemas import (
    AdminKeyUpdate, AdminPaymentManualConfirmRequest, AdminUserUpdate,
    AnnouncementCreate, AnnouncementUpdate,
    RedemptionGenerateRequest, RedemptionGenerateResponse,
)
from .config import settings as _settings
from .router import registry as model_registry
from .security import decrypt_api_key, encrypt_api_key, generate_api_key, generate_id, hash_key, require_admin


router = APIRouter(prefix="/admin", tags=["admin"])
ADMIN_UPLOAD_ROOT = Path(_settings.admin_upload_dir)


def _configured(value: Optional[str]) -> bool:
    return bool((value or "").strip())


def _key_fingerprint(key_hash: str) -> str:
    if not key_hash:
        return ""
    return key_hash[:12]


def _recover_raw_key(encrypted_key: Optional[str]) -> Optional[str]:
    if not encrypted_key:
        return None
    try:
        return decrypt_api_key(encrypted_key)
    except Exception:
        return None


def admin_guard(request: Request):
    require_admin(request)


@router.post("/uploads/station-payout-proof", dependencies=[Depends(admin_guard)])
async def upload_station_payout_proof(file: UploadFile = File(...)):
    content_type = (file.content_type or "").lower()
    if content_type not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unsupported file type")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="empty file")
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="file too large")

    ext = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }[content_type]
    target_dir = ADMIN_UPLOAD_ROOT / "station-payout-proofs"
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(8)}{ext}"
    target_path = target_dir / filename
    target_path.write_bytes(data)

    return {
        "success": True,
        "url": f"/admin-uploads/station-payout-proofs/{filename}",
        "filename": filename,
        "content_type": content_type,
        "size": len(data),
    }


@router.get("/ui")
async def admin_ui(token: str = ""):
    if token != _settings.admin_token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    ui_path = Path(__file__).parent / "static" / "admin.html"
    return FileResponse(ui_path)


@router.get("/users", dependencies=[Depends(admin_guard)])
async def list_users(search: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    query = (
        select(User, StationCustomerLink, Station)
        .outerjoin(StationCustomerLink, StationCustomerLink.user_id == User.id)
        .outerjoin(Station, Station.id == StationCustomerLink.station_id)
        .order_by(User.created_at.desc())
    )
    if search:
        if search.startswith(_settings.key_prefix):
            key_hash_val = hash_key(search)
            key_row = (await db.execute(
                select(ApiKey.user_id).where(ApiKey.key_hash == key_hash_val)
            )).scalar_one_or_none()
            if key_row:
                query = query.where(User.id == key_row)
            else:
                return []
        else:
            pat = f"%{search}%"
            query = query.where(
                User.username.ilike(pat)
                | User.email.ilike(pat)
                | User.external_id.ilike(pat)
                | User.id.ilike(pat)
            )
    result = await db.execute(query.limit(200))
    rows = result.all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "email": getattr(u, "email", None),
            "email_verified_at": getattr(u, "email_verified_at", None),
            "external_id": u.external_id,
            "status": u.status,
            "balance": u.balance,
            "token_limit": u.token_limit,
            "token_used": u.token_used,
            "input_tokens_used": u.input_tokens_used,
            "output_tokens_used": u.output_tokens_used,
            "request_limit_per_minute": u.request_limit_per_minute,
            "request_limit_per_day": u.request_limit_per_day,
            "referral_code": u.referral_code,
            "referred_by": u.referred_by,
            "created_at": u.created_at,
            "updated_at": u.updated_at,
            "station_attribution": None if not station else {
                "station_id": station.id,
                "station_name": station.display_name,
                "station_owner_user_id": station.owner_user_id,
                "link_status": getattr(link, "status", None),
            },
        }
        for u, link, station in rows
    ]


@router.patch("/users/{user_id}", dependencies=[Depends(admin_guard)])
async def update_user(user_id: str, payload: AdminUserUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    if payload.status:
        user.status = payload.status
    if payload.balance is not None:
        balance_delta = int(payload.balance) - int(user.balance or 0)
        user.balance = payload.balance
    else:
        balance_delta = 0
    if payload.token_limit is not None:
        user.token_limit = payload.token_limit
    if payload.token_used is not None:
        user.token_used = payload.token_used
    if payload.input_tokens_used is not None:
        user.input_tokens_used = payload.input_tokens_used
    if payload.output_tokens_used is not None:
        user.output_tokens_used = payload.output_tokens_used
    if payload.request_limit_per_minute is not None:
        user.request_limit_per_minute = payload.request_limit_per_minute
    if payload.request_limit_per_day is not None:
        user.request_limit_per_day = payload.request_limit_per_day

    if balance_delta > 0:
        from .finance_summary import increment_finance_summary
        await ensure_finance_summary_initialized(db, user.id, commit=False)
        await increment_finance_summary(db, user.id, ops_credit_cents=balance_delta)
    elif balance_delta < 0:
        from .finance_summary import increment_finance_summary
        await ensure_finance_summary_initialized(db, user.id, commit=False)
        await increment_finance_summary(db, user.id, ops_debit_cents=abs(balance_delta))

    await db.commit()
    return {
        "id": user.id,
        "status": user.status,
        "balance": user.balance,
        "token_limit": user.token_limit,
        "token_used": user.token_used,
        "input_tokens_used": user.input_tokens_used,
        "output_tokens_used": user.output_tokens_used,
        "request_limit_per_minute": user.request_limit_per_minute,
        "request_limit_per_day": user.request_limit_per_day,
    }


@router.post("/users/{user_id}/reset-usage", dependencies=[Depends(admin_guard)])
async def reset_user_usage(user_id: str, db: AsyncSession = Depends(get_db)):
    """重置用户的 token 使用量为 0"""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    old_values = {
        "token_used": user.token_used,
        "input_tokens_used": user.input_tokens_used,
        "output_tokens_used": user.output_tokens_used,
    }
    user.token_used = 0
    user.input_tokens_used = 0
    user.output_tokens_used = 0
    await db.commit()
    
    return {
        "id": user.id,
        "before": old_values,
        "after": {
            "token_used": 0,
            "input_tokens_used": 0,
            "output_tokens_used": 0,
        },
        "message": "usage reset successfully"
    }


@router.get("/users/{user_id}", dependencies=[Depends(admin_guard)])
async def get_user_detail(user_id: str, db: AsyncSession = Depends(get_db)):
    """获取用户详情，包含该用户的所有 Key"""
    result = await db.execute(
        select(User, StationCustomerLink, Station)
        .outerjoin(StationCustomerLink, StationCustomerLink.user_id == User.id)
        .outerjoin(Station, Station.id == StationCustomerLink.station_id)
        .where(User.id == user_id)
    )
    row = result.first()
    user = row[0] if row else None
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    link = row[1] if row else None
    station = row[2] if row else None
    
    # 获取用户的所有 Key
    keys_result = await db.execute(
        select(ApiKey).where(ApiKey.user_id == user_id).order_by(ApiKey.created_at.desc())
    )
    keys = keys_result.scalars().all()
    
    return {
        "id": user.id,
        "username": user.username,
        "email": getattr(user, "email", None),
        "email_verified_at": getattr(user, "email_verified_at", None),
        "external_id": user.external_id,
        "status": user.status,
        "balance": user.balance,
        "balance_usd": user.balance / 100,  # 分转美元
        "token_limit": user.token_limit,
        "token_used": user.token_used,
        "input_tokens_used": user.input_tokens_used,
        "output_tokens_used": user.output_tokens_used,
        "request_limit_per_minute": user.request_limit_per_minute,
        "request_limit_per_day": user.request_limit_per_day,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
        "finance_summary": await build_user_finance_snapshot(db, user.id, user.balance),
        "station_attribution": None if not station else {
            "station_id": station.id,
            "station_name": station.display_name,
            "station_slug": station.slug,
            "station_owner_user_id": station.owner_user_id,
            "station_status": station.status,
            "link_id": getattr(link, "id", None),
            "link_status": getattr(link, "status", None),
            "linked_at": getattr(link, "created_at", None),
        },
        "keys": [
            {
                "id": k.id,
                "kind": k.kind,
                "status": k.status,
                "fingerprint": _key_fingerprint(k.key_hash),
                "raw_key": _recover_raw_key(k.encrypted_key),
                "shared_balance": user.balance,
                "shared_balance_usd": user.balance / 100,
                "created_at": k.created_at,
                "last_used_at": k.last_used_at,
            }
            for k in keys
        ],
        "key_display_policy": {
            "raw_key_recoverable": True,
            "shared_balance_scope": "user",
            "message": "New keys are stored encrypted for admin recovery. Older keys created before this change may still be unrecoverable. All keys under the same user share one balance.",
        },
    }


@router.post("/users/{user_id}/keys", dependencies=[Depends(admin_guard)])
async def create_user_key(user_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    api_key_value = generate_api_key()
    key = ApiKey(
        id=generate_id("k_"),
        user_id=user.id,
        key_hash=hash_key(api_key_value),
        encrypted_key=encrypt_api_key(api_key_value),
        status="active",
        created_at=datetime.utcnow(),
    )
    db.add(key)
    await db.commit()

    return {"id": key.id, "api_key": api_key_value, "status": key.status}


@router.patch("/keys/{key_id}", dependencies=[Depends(admin_guard)])
async def update_key(key_id: str, payload: AdminKeyUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ApiKey).where(ApiKey.id == key_id))
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="key not found")

    if payload.status:
        key.status = payload.status

    await db.commit()
    try:
        from .proxy import key_cache

        await key_cache.delete(key.key_hash)
    except Exception:
        pass
    return {"id": key.id, "status": key.status}


@router.get("/usage/daily", dependencies=[Depends(admin_guard)])
async def list_daily_usage(
    user_id: Optional[str] = None,
    day: Optional[date] = None,
    db: AsyncSession = Depends(get_db),
):
    query = select(UsageDaily, User).join(User, UsageDaily.user_id == User.id)
    if user_id:
        query = query.where(UsageDaily.user_id == user_id)
    if day:
        query = query.where(UsageDaily.day == day)
    result = await db.execute(query.order_by(UsageDaily.day.desc()).limit(200))
    rows = result.all()
    return [
        {
            "user_id": usage.user_id,
            "day": usage.day,
            "tokens_total": usage.tokens_total,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "images_total": getattr(usage, "images_total", 0),
            "cost_cents": usage.cost_cents,
            "cost_usd": usage.cost_cents / 100,  # 分转美元
            "requests_total": usage.requests_total,
            "username": user.username,
            "external_id": user.external_id,
        }
        for usage, user in rows
    ]


@router.get("/metrics/summary", dependencies=[Depends(admin_guard)])
async def summary_metrics(db: AsyncSession = Depends(get_db)):
    total_users = await db.scalar(select(func.count()).select_from(User))
    active_users = await db.scalar(select(func.count()).select_from(User).where(User.status == "active"))
    total_tokens = await db.scalar(select(func.coalesce(func.sum(User.token_used), 0)))

    today = date.today()
    total_requests_today = await db.scalar(
        select(func.coalesce(func.sum(UsageDaily.requests_total), 0)).where(UsageDaily.day == today)
    )
    total_images_today = await db.scalar(
        select(func.coalesce(func.sum(UsageDaily.images_total), 0)).where(UsageDaily.day == today)
    )
    paid_today_cents = await db.scalar(
        select(func.coalesce(func.sum(PaymentOrder.add_balance_cents), 0)).where(
            PaymentOrder.status == "confirmed",
            func.date(PaymentOrder.confirmed_at) == today,
        )
    )
    consumed_today_cents = await db.scalar(
        select(func.coalesce(func.sum(UsageDaily.cost_cents), 0)).where(UsageDaily.day == today)
    )

    return {
        "total_users": int(total_users or 0),
        "active_users": int(active_users or 0),
        "total_tokens": int(total_tokens or 0),
        "total_requests_today": int(total_requests_today or 0),
        "total_images_today": int(total_images_today or 0),
        "paid_today_cents": int(paid_today_cents or 0),
        "paid_today_usd": int(paid_today_cents or 0) / 100,
        "consumed_today_cents": int(consumed_today_cents or 0),
        "consumed_today_usd": int(consumed_today_cents or 0) / 100,
        "net_today_cents": int((paid_today_cents or 0) - (consumed_today_cents or 0)),
        "net_today_usd": int((paid_today_cents or 0) - (consumed_today_cents or 0)) / 100,
    }


@router.get("/finance/summary", dependencies=[Depends(admin_guard)])
async def finance_summary(
    search: Optional[str] = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    limit = max(1, min(limit, 200))
    query = select(User).order_by(User.created_at.desc())
    if search:
        pat = f"%{search}%"
        query = query.where(
            User.username.ilike(pat)
            | User.email.ilike(pat)
            | User.external_id.ilike(pat)
            | User.id.ilike(pat)
        )
    users = (await db.execute(query.limit(limit))).scalars().all()
    snapshots = await build_user_finance_snapshots(
        db,
        {user.id: int(user.balance or 0) for user in users},
    )
    return [
        {
            "user_id": user.id,
            "username": user.username,
            "email": getattr(user, "email", None),
            "email_verified_at": getattr(user, "email_verified_at", None),
            "external_id": user.external_id,
            "created_at": user.created_at,
            "status": user.status,
            "finance_summary": snapshots.get(user.id, {}),
        }
        for user in users
    ]


@router.get("/keys", dependencies=[Depends(admin_guard)])
async def list_keys(user_id: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    """列出所有 Key"""
    query = select(ApiKey, User).join(User, ApiKey.user_id == User.id)
    if user_id:
        query = query.where(ApiKey.user_id == user_id)
    result = await db.execute(query.order_by(ApiKey.created_at.desc()).limit(200))
    rows = result.all()
    return [
        {
            "id": key.id,
            "user_id": key.user_id,
            "username": user.username,
            "external_id": user.external_id,
            "kind": key.kind,
            "status": key.status,
            "fingerprint": _key_fingerprint(key.key_hash),
            "raw_key": _recover_raw_key(key.encrypted_key),
            "shared_balance": user.balance,
            "shared_balance_usd": user.balance / 100,
            "created_at": key.created_at,
            "last_used_at": key.last_used_at,
        }
        for key, user in rows
    ]


@router.get("/ops/health", dependencies=[Depends(admin_guard)])
async def ops_health(db: AsyncSession = Depends(get_db)):
    now = datetime.utcnow()
    since = now - timedelta(hours=24)
    total_requests = (
        await db.execute(select(func.count()).select_from(RequestLog).where(RequestLog.created_at >= since))
    ).scalar() or 0
    failed_requests = (
        await db.execute(
            select(func.count()).select_from(RequestLog).where(RequestLog.created_at >= since, RequestLog.status_code >= 400)
        )
    ).scalar() or 0
    latest_success = (
        await db.execute(
            select(RequestLog.created_at)
            .where(RequestLog.status_code < 400)
            .order_by(RequestLog.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    status_rows = (
        await db.execute(
            select(RequestLog.status_code, func.count())
            .where(RequestLog.created_at >= since, RequestLog.status_code >= 400)
            .group_by(RequestLog.status_code)
            .order_by(func.count().desc())
            .limit(8)
        )
    ).all()
    model_rows = (
        await db.execute(
            select(RequestLog.model, func.count())
            .where(RequestLog.created_at >= since, RequestLog.status_code >= 400)
            .group_by(RequestLog.model)
            .order_by(func.count().desc())
            .limit(8)
        )
    ).all()
    failed_rows = (
        await db.execute(
            select(RequestLog, User)
            .join(User, RequestLog.user_id == User.id)
            .where(RequestLog.status_code >= 400)
            .order_by(RequestLog.created_at.desc())
            .limit(20)
        )
    ).all()

    model_registry.ensure_initialized()
    public_models = model_registry.list_public_models()
    default_text = getattr(model_registry, "default_text_model_id", "") or None
    default_image = getattr(model_registry, "default_image_model_id", "") or None

    env_checks = {
        "railway_environment": _configured(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_ENVIRONMENT_NAME")),
        "port": _configured(os.getenv("PORT")),
        "database": _configured(_settings.database_url)
        or (_configured(_settings.db_host) and _configured(_settings.db_name) and _configured(_settings.db_user)),
        "self_base_url": _configured(_settings.self_base_url),
        "gateway_base_url": _configured(_settings.gateway_base_url),
        "gateway_api_key": _configured(_settings.gateway_api_key),
        "model_catalog": _configured(_settings.model_catalog_json) or Path(_settings.model_catalog_path).exists(),
        "email": _configured(_settings.resend_api_key),
        "payment": epay_configured(),
        "monitoring": _configured(_settings.monitoring_token),
        "gateway_health_url": _configured(_settings.monitoring_gateway_health_url),
    }

    return {
        "generated_at": now,
        "window_hours": 24,
        "traffic": {
            "total_requests": int(total_requests),
            "failed_requests": int(failed_requests),
            "error_rate": (float(failed_requests) / float(total_requests)) if total_requests else 0,
            "latest_success_at": latest_success,
        },
        "errors": {
            "by_status": [{"status_code": int(code), "count": int(count)} for code, count in status_rows],
            "by_model": [{"model": model or "-", "count": int(count)} for model, count in model_rows],
            "recent": [
                {
                    "created_at": log.created_at,
                    "user": user.username or user.email or user.external_id or user.id,
                    "status_code": log.status_code,
                    "endpoint": log.endpoint,
                    "model": log.model,
                    "duration_ms": log.duration_ms,
                    "route_reason": log.route_reason,
                    "upstream_request_id": log.upstream_request_id,
                }
                for log, user in failed_rows
            ],
        },
        "models": {
            "count": len(public_models),
            "default_text": default_text,
            "default_image": default_image,
            "routable": model_registry.has_routable_models(),
        },
        "config": env_checks,
    }


@router.get("/recharges", dependencies=[Depends(admin_guard)])
async def list_recharges(user_id: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    """列出充值记录"""
    query = select(RechargeLog, User).join(User, RechargeLog.user_id == User.id)
    if user_id:
        query = query.where(RechargeLog.user_id == user_id)
    result = await db.execute(query.order_by(RechargeLog.created_at.desc()).limit(200))
    rows = result.all()
    return [
        {
            "id": log.id,
            "order_id": log.order_id,
            "user_id": log.user_id,
            "username": user.username,
            "external_id": user.external_id,
            "amount": log.amount,
            "balance_added": log.balance_added,
            "balance_added_usd": log.balance_added / 100,  # 分转美元
            "tokens_added": log.tokens_added,
            "daily_requests_added": log.daily_requests_added,
            "note": log.note,
            "created_at": log.created_at,
        }
        for log, user in rows
    ]


@router.get("/users/{user_id}/request-logs", dependencies=[Depends(admin_guard)])
async def list_user_request_logs(
    user_id: str,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """查询用户的请求明细日志"""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    count_result = await db.execute(
        select(func.count()).select_from(RequestLog).where(RequestLog.user_id == user_id)
    )
    total = count_result.scalar() or 0

    result = await db.execute(
        select(RequestLog)
        .where(RequestLog.user_id == user_id)
        .order_by(RequestLog.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    logs = result.scalars().all()

    return {
        "user_id": user_id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "data": [
            {
                "created_at": (log.created_at.isoformat() + "Z") if log.created_at else None,
                "endpoint": log.endpoint,
                "model": getattr(log, "customer_model_alias", "") or log.model,
                "provider_model": getattr(log, "provider_model", "") or log.model,
                "customer_model_alias": getattr(log, "customer_model_alias", "") or log.model,
                "input_tokens": log.input_tokens,
                "output_tokens": log.output_tokens,
                "cached_tokens": getattr(log, "cached_tokens", 0),
                "cache_read_tokens": getattr(log, "cache_read_tokens", 0) or getattr(log, "cached_tokens", 0),
                "cache_creation_tokens": getattr(log, "cache_creation_tokens", 0),
                "image_count": getattr(log, "image_count", 0),
                "usage_unit_type": getattr(log, "usage_unit_type", "tokens"),
                "usage_unit_count": getattr(log, "usage_unit_count", 0),
                "billable_sku": getattr(log, "billable_sku", "") or (getattr(log, "customer_model_alias", "") or log.model),
                "upstream_request_id": getattr(log, "upstream_request_id", ""),
                "total_tokens": log.input_tokens + log.output_tokens,
                "cost_cents": log.cost_cents,
                "cost_usd": log.cost_cents / 100,
                "duration_ms": log.duration_ms,
                "status_code": log.status_code,
                "route_reason": getattr(log, "route_reason", ""),
            }
            for log in logs
        ],
    }


# ============== Redemption Code Management ==============

def _generate_code() -> str:
    parts = [secrets.token_hex(2).upper() for _ in range(4)]
    return f"CC-{parts[0]}-{parts[1]}-{parts[2]}-{parts[3]}"


@router.post("/redemption-codes/generate", dependencies=[Depends(admin_guard)],
             response_model=RedemptionGenerateResponse)
async def generate_redemption_codes(
    payload: RedemptionGenerateRequest, db: AsyncSession = Depends(get_db)
):
    codes = []
    for _ in range(payload.count):
        code_str = _generate_code()
        code = RedemptionCode(
            id=generate_id("rc_"),
            code=code_str,
            balance_cents=payload.balance_cents,
            status="unused",
        )
        db.add(code)
        codes.append(code_str)
    await db.commit()
    return RedemptionGenerateResponse(
        codes=codes, balance_cents=payload.balance_cents, count=payload.count
    )


@router.get("/redemption-codes", dependencies=[Depends(admin_guard)])
async def list_redemption_codes(
    status_filter: Optional[str] = None, db: AsyncSession = Depends(get_db)
):
    query = select(RedemptionCode).order_by(RedemptionCode.created_at.desc())
    if status_filter:
        query = query.where(RedemptionCode.status == status_filter)
    result = await db.execute(query.limit(200))
    codes = result.scalars().all()
    return [
        {
            "id": c.id,
            "code": c.code,
            "balance_cents": c.balance_cents,
            "status": c.status,
            "used_by": c.used_by,
            "used_at": c.used_at,
            "created_at": c.created_at,
        }
        for c in codes
    ]


@router.patch("/redemption-codes/{code_id}", dependencies=[Depends(admin_guard)])
async def disable_redemption_code(code_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(RedemptionCode).where(RedemptionCode.id == code_id))
    code = result.scalar_one_or_none()
    if not code:
        raise HTTPException(status_code=404, detail="code not found")
    code.status = "disabled"
    await db.commit()
    return {"id": code.id, "status": code.status}


# ============== Announcement Management ==============

@router.post("/announcements", dependencies=[Depends(admin_guard)])
async def create_announcement(payload: AnnouncementCreate, db: AsyncSession = Depends(get_db)):
    ann = Announcement(
        id=generate_id("ann_"),
        title=payload.title,
        content=payload.content,
        priority=payload.priority,
        display_type=payload.display_type,
        audience=payload.audience,
        cta_label=payload.cta_label or "",
        cta_value=payload.cta_value or "",
        image_url=payload.image_url or "",
        status="active",
    )
    db.add(ann)
    await db.commit()
    return {"id": ann.id, "title": ann.title, "status": ann.status}


@router.get("/announcements", dependencies=[Depends(admin_guard)])
async def list_announcements_admin(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Announcement).order_by(Announcement.created_at.desc()).limit(50)
    )
    anns = result.scalars().all()
    return [
        {
            "id": a.id,
            "title": a.title,
            "content": a.content,
            "priority": a.priority,
            "display_type": getattr(a, "display_type", "banner") or "banner",
            "audience": getattr(a, "audience", "all") or "all",
            "cta_label": getattr(a, "cta_label", "") or "",
            "cta_value": getattr(a, "cta_value", "") or "",
            "image_url": getattr(a, "image_url", "") or "",
            "status": a.status,
            "created_at": a.created_at,
        }
        for a in anns
    ]


@router.patch("/announcements/{ann_id}", dependencies=[Depends(admin_guard)])
async def update_announcement(
    ann_id: str, payload: AnnouncementUpdate, db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Announcement).where(Announcement.id == ann_id))
    ann = result.scalar_one_or_none()
    if not ann:
        raise HTTPException(status_code=404, detail="announcement not found")
    if payload.title is not None:
        ann.title = payload.title
    if payload.content is not None:
        ann.content = payload.content
    if payload.priority is not None:
        ann.priority = payload.priority
    if payload.display_type is not None:
        ann.display_type = payload.display_type
    if payload.audience is not None:
        ann.audience = payload.audience
    if payload.cta_label is not None:
        ann.cta_label = payload.cta_label or ""
    if payload.cta_value is not None:
        ann.cta_value = payload.cta_value or ""
    if payload.image_url is not None:
        ann.image_url = payload.image_url or ""
    if payload.status is not None:
        ann.status = payload.status
    await db.commit()
    return {"id": ann.id, "title": ann.title, "status": ann.status}


# ============== Payment Order Management ==============

@router.get("/payment-orders", dependencies=[Depends(admin_guard)])
async def list_payment_orders(
    status_filter: Optional[str] = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    query = select(PaymentOrder).order_by(PaymentOrder.created_at.desc())
    if status_filter:
        query = query.where(PaymentOrder.status == status_filter)
    result = await db.execute(query.limit(limit))
    orders = result.scalars().all()
    return [
        {
            "id": o.id,
            "user_id": o.user_id,
            "order_no": o.order_no,
            "amount_rmb": o.amount_rmb,
            "add_balance_cents": o.add_balance_cents,
            "status": o.status,
            "trade_no": o.trade_no,
            "pay_url": o.pay_url,
            "created_at": o.created_at,
            "confirmed_at": o.confirmed_at,
        }
        for o in orders
    ]


@router.post("/payment-orders/{order_no}/force-confirm", dependencies=[Depends(admin_guard)])
async def force_confirm_order(order_no: str, db: AsyncSession = Depends(get_db)):
    """Admin 手动补单：查询支付服务验证后强制入账。"""
    from .payment import _confirm_with_query_fallback

    order = (
        await db.execute(select(PaymentOrder).where(PaymentOrder.order_no == order_no))
    ).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="order not found")
    if order.status == "confirmed":
        return {"order_no": order_no, "status": "already_confirmed"}

    try:
        result = await _confirm_with_query_fallback(order_no, db)
    except HTTPException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {
        "order_no": order_no,
        "status": "already_confirmed" if result.get("already_confirmed") else "confirmed",
        "trade_no": result["order"].trade_no,
        "added_cents": result["added_cents"],
        "new_balance": result["user"].balance,
        "new_balance_usd": result["user"].balance / 100,
    }


@router.post("/payment-orders/{order_no}/manual-confirm", dependencies=[Depends(admin_guard)])
async def manual_confirm_order(
    order_no: str,
    payload: AdminPaymentManualConfirmRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Admin 手工补单：当支付服务不给查单接口或没有回调到 CoinCoin 时，
    允许管理员基于支付成功回跳 URL 手工确认 pending 订单。
    """
    order = (
        await db.execute(
            select(PaymentOrder)
            .where(PaymentOrder.order_no == order_no)
        )
    ).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="order not found")
    if order.status == "confirmed":
        return {"order_no": order_no, "status": "already_confirmed"}

    try:
        callback = verify_epay_callback_params(
            extract_epay_params_from_proof_url(payload.proof_url),
            require_success=True,
        )
    except EpayVerificationError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc

    if callback["out_trade_no"] != order_no:
        raise HTTPException(status_code=400, detail="payment proof does not match this order")

    try:
        result = await confirm_paid_order(
            order_no=order_no,
            money=callback["money"],
            trade_no=callback["trade_no"],
            db=db,
        )
    except PaymentConfirmError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    user = result["user"]

    return {
        "order_no": order_no,
        "status": "already_confirmed" if result.get("already_confirmed") else "confirmed",
        "trade_no": callback["trade_no"],
        "added_cents": result["added_cents"],
        "new_balance": user.balance,
        "new_balance_usd": user.balance / 100,
    }


# ============== Referral Rewards ==============

@router.get("/referral-rewards", dependencies=[Depends(admin_guard)])
async def list_referral_rewards(
    referrer_id: Optional[str] = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(ReferralReward, User)
        .join(User, ReferralReward.referrer_id == User.id)
        .order_by(ReferralReward.created_at.desc())
    )
    if referrer_id:
        query = query.where(ReferralReward.referrer_id == referrer_id)
    result = await db.execute(query.limit(limit))
    rows = result.all()
    return [
        {
            "id": r.id,
            "referrer_id": r.referrer_id,
            "referrer_username": u.username,
            "referred_id": r.referred_id,
            "recipient_id": getattr(r, "recipient_id", None) or r.referrer_id,
            "reward_type": getattr(r, "reward_type", None) or "purchase_commission",
            "order_no": r.order_no,
            "order_amount_cents": r.order_amount_cents,
            "reward_cents": r.reward_cents,
            "reward_usd": r.reward_cents / 100,
            "created_at": r.created_at,
        }
        for r, u in rows
    ]


@router.get("/referral-stats", dependencies=[Depends(admin_guard)])
async def referral_stats(db: AsyncSession = Depends(get_db)):
    total_rewards = await db.scalar(
        select(func.coalesce(func.sum(ReferralReward.reward_cents), 0))
    ) or 0
    total_referrals = await db.scalar(
        select(func.count()).select_from(User).where(User.referred_by.isnot(None))
    ) or 0
    total_referrers = await db.scalar(
        select(func.count(func.distinct(ReferralReward.referrer_id)))
    ) or 0
    return {
        "total_rewards_cents": total_rewards,
        "total_rewards_usd": total_rewards / 100,
        "total_referred_users": total_referrals,
        "total_active_referrers": total_referrers,
    }
