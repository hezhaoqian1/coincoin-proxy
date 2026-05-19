import asyncio
import json
import secrets
import time
from datetime import UTC, date, datetime, time as dt_time, timedelta
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from .config import settings
from .db import get_db
from . import gemini_cpa
from .prompt_cache import build_claude_code_prompt_cache_key
from .proxy import (
    _build_upstream_headers, _ensure_content_text, _sanitize_encrypted_ids,
    _collect_responses_event_stream_payload, _responses_payload_is_empty_success,
    _chat_completion_chunk_line,
    _normalize_openai_base_url, _responses_tools_to_chat_tools,
    _translate_chat_response_to_responses,
    authenticate_user, authorize_request, extract_upstream_request_id,
    filter_headers, get_http_client, get_stream_client, proxy_images_edits, proxy_images_generations,
    proxy_responses, responses_health, _KEY_ID_ATTR,
)
from .router import (
    CLAUDE_COMPAT_PROVIDER_KIRO_GO,
    ModelCapabilityError,
    UnknownModelError,
    registry as model_registry,
)
from .schemas import BalanceResponse, ReferralCodeUpdateRequest
from .station_runtime import resolve_station_model_for_user, usage_pricing_kwargs, user_station_context
from .usage_buffer import (
    china_today,
    extract_cache_creation_tokens,
    extract_cache_read_tokens,
    extract_total_input_tokens,
    usage_buffer,
)
from .finance_summary import ensure_finance_summary_initialized, increment_finance_summary
from .models import ReferralReward, User
from .referral import (
    REWARD_PURCHASE_COMMISSION,
    build_referral_record,
)


router = APIRouter(prefix="/v1", tags=["openai-compat"])


# ============== 标准 OpenAI 错误格式 ==============
def openai_error(message: str, error_type: str = "invalid_request_error", 
                 param: Optional[str] = None, code: Optional[str] = None, 
                 status_code: int = 400) -> JSONResponse:
    """返回标准 OpenAI 错误格式"""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "param": param,
                "code": code,
            }
        }
    )


def _model_resolution_to_openai_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, UnknownModelError):
        return openai_error(str(exc), "invalid_request_error", param="model", code="model_not_found", status_code=400)
    if isinstance(exc, ModelCapabilityError):
        return openai_error(str(exc), "invalid_request_error", param="model", code="model_capability_mismatch", status_code=400)
    return openai_error("Unable to resolve model", "server_error", code="model_resolution_failed", status_code=500)


def _parse_usage_datetime_filter(value: Optional[str], *, is_end: bool = False):
    raw = (value or "").strip()
    if not raw:
        return None, False
    try:
        if len(raw) == 10:
            day = date.fromisoformat(raw)
            china_boundary = datetime.combine(
                day + (timedelta(days=1) if is_end else timedelta()),
                dt_time.min,
            )
            return china_boundary - timedelta(hours=8), is_end

        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed, False
    except ValueError:
        return None, False


def _serialize_public_model(public_model) -> Dict[str, Any]:
    default_for = []
    if public_model.public_id == model_registry.default_text_model_id:
        default_for.append("text")
    if public_model.public_id == getattr(model_registry, "default_embedding_model_id", ""):
        default_for.append("embedding")
    if public_model.public_id == model_registry.default_image_model_id:
        default_for.append("image")
    explicit_cached_price = getattr(public_model, "effective_cached_input_per_million", None)
    cached_input_price = round(
        float(explicit_cached_price if explicit_cached_price is not None else float(public_model.price_input_per_million or 0) * float(settings.cache_discount_rate or 0)),
        4,
    )
    return {
        "id": public_model.public_id,
        "object": "model",
        "created": public_model.created,
        "owned_by": public_model.owned_by,
        "coincoin_capabilities": list(public_model.capabilities),
        "coincoin_billable_sku": public_model.billable_sku,
        "coincoin_routing_mode": public_model.routing_mode,
        "coincoin_delivery_lane": public_model.delivery_lane,
        "coincoin_default_for": default_for,
        "coincoin_metadata": dict(public_model.metadata or {}),
        "coincoin_price_input_per_million": public_model.price_input_per_million,
        "coincoin_price_cached_input_per_million": cached_input_price,
        "coincoin_price_output_per_million": public_model.price_output_per_million,
        "coincoin_price_per_image_cents": public_model.price_per_image_cents,
        "coincoin_base_price_input_per_million": getattr(public_model, "base_price_input_per_million", 0),
        "coincoin_base_price_output_per_million": getattr(public_model, "base_price_output_per_million", 0),
        "coincoin_base_price_per_image_cents": getattr(public_model, "base_price_per_image_cents", 0.0),
        "coincoin_pricing_mode": getattr(public_model, "pricing_mode", "explicit_price"),
        "coincoin_model_multiplier": getattr(public_model, "model_multiplier", 1.0),
        "coincoin_output_multiplier": getattr(public_model, "output_multiplier", 1.0),
        "coincoin_cache_read_multiplier": getattr(public_model, "cache_read_multiplier", 0.0),
        "coincoin_image_multiplier": getattr(public_model, "image_multiplier", 1.0),
        "coincoin_price_version": getattr(public_model, "price_version", 0),
    }


@router.get("/balance", response_model=BalanceResponse)
async def get_balance(request: Request, db: AsyncSession = Depends(get_db)):
    """
    查询账户余额和使用量
    
    使用自己的 API Key 认证，返回余额、token 用量和价格信息。
    注：直接从数据库读取最新数据，不使用缓存。
    """
    try:
        cached_user = await authenticate_user(request, db)
    except HTTPException as e:
        if e.status_code == 401:
            return openai_error("Invalid API key provided", "authentication_error", code="invalid_api_key", status_code=401)
        elif e.status_code == 403:
            return openai_error("Access denied", "permission_error", code="access_denied", status_code=403)
        raise
    
    # 直接从数据库查询最新用户数据（不用缓存）
    from .models import User
    from sqlalchemy import select
    result = await db.execute(select(User).where(User.id == cached_user.id))
    user = result.scalar_one_or_none()
    if not user:
        return openai_error("User not found", "authentication_error", code="user_not_found", status_code=404)
    
    # 获取待刷新的数据
    pending_tokens = await usage_buffer.get_pending_tokens(user.id)
    pending_cost = await usage_buffer.get_pending_cost(user.id)
    from .billing import get_available_balance_cents, serialize_billing_state
    billing_snapshot = await get_available_balance_cents(db, user, pending_cost_cents=pending_cost)
    
    # 计算当前值（数据库最新值 + 待刷新）
    balance = int(billing_snapshot.get("available_cents", 0))
    token_used = (user.token_used or 0) + pending_tokens
    input_tokens_used = user.input_tokens_used or 0
    output_tokens_used = user.output_tokens_used or 0
    token_limit = user.token_limit
    
    # 计算剩余 tokens
    token_remaining = None
    if token_limit is not None:
        token_remaining = max(0, token_limit - token_used)
    
    station_models = []
    station_context = user_station_context(cached_user)
    if station_context:
        try:
            from .stations import get_station_public_models_by_id
            station_models = await get_station_public_models_by_id(str(station_context.get("station_id") or ""), db)
        except Exception:
            station_models = []
    default_station_model = next(
        (model for model in station_models if "text" in (model.get("coincoin_default_for") or [])),
        station_models[0] if station_models else None,
    )
    price_input_per_million = settings.price_input_per_million / 100
    price_cached_input_per_million = (settings.price_input_per_million * settings.cache_discount_rate) / 100
    price_output_per_million = settings.price_output_per_million / 100
    pricing_scope = "official"
    pricing_model_id = None
    station_slug = None
    station_display_name = None
    if default_station_model:
        pricing_scope = "station"
        pricing_model_id = default_station_model.get("id")
        station_slug = station_context.get("slug") or None
        station_display_name = station_context.get("display_name") or None
        price_input_per_million = float(default_station_model.get("coincoin_price_input_per_million") or 0) / 100
        price_cached_input_per_million = float(default_station_model.get("coincoin_price_cached_input_per_million") or 0) / 100
        price_output_per_million = float(default_station_model.get("coincoin_price_output_per_million") or 0) / 100

    return BalanceResponse(
        user_id=user.id,
        balance=balance,
        balance_usd=balance / 100,  # 分转美元
        token_used=token_used,
        input_tokens_used=input_tokens_used,
        output_tokens_used=output_tokens_used,
        token_limit=token_limit,
        token_remaining=token_remaining,
        price_input_per_million=price_input_per_million,
        price_cached_input_per_million=price_cached_input_per_million,
        price_output_per_million=price_output_per_million,
        pricing_scope=pricing_scope,
        pricing_model_id=pricing_model_id,
        station_id=station_context.get("station_id") or None,
        station_slug=station_slug,
        station_display_name=station_display_name,
        station_pricing_models=station_models if station_models else None,
        billing=serialize_billing_state(
            billing_snapshot.get("subscription"),
            billing_snapshot.get("traffic_packs") or [],
            user,
        ),
    )


@router.get("/usage")
async def get_usage(
    request: Request,
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
    endpoint: Optional[str] = None,
    status_code: Optional[int] = None,
    api_key_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    end_exclusive: bool = False,
):
    try:
        cached_user = await authenticate_user(request, db)
    except HTTPException as e:
        if e.status_code == 401:
            return openai_error("Invalid API key provided", "authentication_error", code="invalid_api_key", status_code=401)
        elif e.status_code == 403:
            return openai_error("Access denied", "permission_error", code="access_denied", status_code=403)
        raise

    from .models import RequestLog
    from sqlalchemy import select, func, and_

    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    conditions = [RequestLog.user_id == cached_user.id]
    if endpoint:
        conditions.append(RequestLog.endpoint == endpoint)
    if status_code is not None:
        conditions.append(RequestLog.status_code == status_code)
    if api_key_id:
        conditions.append(RequestLog.api_key_id == api_key_id)
    if start_date:
        start_bound, _ = _parse_usage_datetime_filter(start_date)
        if start_bound is not None:
            conditions.append(RequestLog.created_at >= start_bound)
    if end_date:
        end_bound, parsed_end_exclusive = _parse_usage_datetime_filter(end_date, is_end=True)
        if end_bound is not None:
            if end_exclusive or parsed_end_exclusive:
                conditions.append(RequestLog.created_at < end_bound)
            else:
                conditions.append(RequestLog.created_at <= end_bound)

    where = and_(*conditions)

    count_result = await db.execute(
        select(func.count()).select_from(RequestLog).where(where)
    )
    total = count_result.scalar() or 0

    summary_result = await db.execute(
        select(
            func.coalesce(func.sum(RequestLog.cost_cents), 0).label("cost_cents"),
            func.coalesce(func.sum(RequestLog.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(RequestLog.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(RequestLog.cached_tokens), 0).label("cached_tokens"),
            func.coalesce(func.sum(RequestLog.cache_read_tokens), 0).label("cache_read_tokens"),
            func.coalesce(func.sum(RequestLog.cache_creation_tokens), 0).label("cache_creation_tokens"),
            func.coalesce(func.sum(RequestLog.image_count), 0).label("image_count"),
            func.coalesce(func.sum(RequestLog.usage_unit_count), 0).label("usage_unit_count"),
        ).where(where)
    )
    summary_row = summary_result.first()

    def _summary_value(index: int, key: str) -> int:
        if summary_row is None:
            return 0
        mapping = getattr(summary_row, "_mapping", None)
        if mapping is not None and key in mapping:
            return int(mapping[key] or 0)
        return int(summary_row[index] or 0)

    summary_cost_cents = _summary_value(0, "cost_cents")
    summary_input_tokens = _summary_value(1, "input_tokens")
    summary_output_tokens = _summary_value(2, "output_tokens")
    summary_cached_tokens = _summary_value(3, "cached_tokens")
    summary_cache_read_tokens = _summary_value(4, "cache_read_tokens")
    summary_cache_creation_tokens = _summary_value(5, "cache_creation_tokens")
    summary_image_count = _summary_value(6, "image_count")
    summary_usage_unit_count = _summary_value(7, "usage_unit_count")
    summary_cache_read_tokens = max(summary_cache_read_tokens, summary_cached_tokens)

    result = await db.execute(
        select(RequestLog)
        .where(where)
        .order_by(RequestLog.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    logs = result.scalars().all()

    return {
        "user_id": cached_user.id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "summary": {
            "cost_cents": summary_cost_cents,
            "cost_usd": summary_cost_cents / 100,
            "input_tokens": summary_input_tokens,
            "output_tokens": summary_output_tokens,
            "cached_tokens": summary_cached_tokens,
            "cache_read_tokens": summary_cache_read_tokens,
            "cache_creation_tokens": summary_cache_creation_tokens,
            "total_tokens": summary_input_tokens + summary_output_tokens,
            "image_count": summary_image_count,
            "usage_unit_count": summary_usage_unit_count,
        },
        "data": [
            {
                "created_at": (log.created_at.isoformat() + "Z") if log.created_at else None,
                "api_key_id": getattr(log, "api_key_id", None),
                "endpoint": log.endpoint,
                "model": getattr(log, "customer_model_alias", "") or log.model,
                "input_tokens": log.input_tokens,
                "output_tokens": log.output_tokens,
                "cached_tokens": getattr(log, "cached_tokens", 0),
                "cache_read_tokens": getattr(log, "cache_read_tokens", 0) or getattr(log, "cached_tokens", 0),
                "cache_creation_tokens": getattr(log, "cache_creation_tokens", 0),
                "image_count": getattr(log, "image_count", 0),
                "usage_unit_type": getattr(log, "usage_unit_type", "tokens"),
                "usage_unit_count": getattr(log, "usage_unit_count", 0),
                "billable_sku": getattr(log, "billable_sku", "") or (getattr(log, "customer_model_alias", "") or log.model),
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


@router.get("/usage/daily")
async def get_daily_usage(request: Request, db: AsyncSession = Depends(get_db), days: int = 7):
    try:
        cached_user = await authenticate_user(request, db)
    except HTTPException as e:
        if e.status_code == 401:
            return openai_error("Invalid API key", "authentication_error", code="invalid_api_key", status_code=401)
        raise

    from .models import UsageDaily
    from sqlalchemy import select

    days = max(1, min(days, 90))
    start = china_today() - timedelta(days=days - 1)

    result = await db.execute(
        select(UsageDaily)
        .where(UsageDaily.user_id == cached_user.id, UsageDaily.day >= start)
        .order_by(UsageDaily.day.asc())
    )
    rows = result.scalars().all()
    return [
        {
            "day": str(r.day),
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "tokens_total": r.tokens_total,
            "images_total": getattr(r, "images_total", 0),
            "cost_cents": r.cost_cents,
            "cost_usd": r.cost_cents / 100,
            "requests_total": r.requests_total,
        }
        for r in rows
    ]


@router.post("/redeem")
async def redeem_code(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        cached_user = await authenticate_user(request, db)
    except HTTPException as e:
        if e.status_code == 401:
            return openai_error("Invalid API key", "authentication_error", code="invalid_api_key", status_code=401)
        raise

    from .models import RedemptionCode, User
    from .rate_limiter import rate_limiter
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from datetime import datetime as dt

    if not await rate_limiter.allow(f"redeem:{cached_user.id}", 6):
        raise HTTPException(status_code=429, detail="too many redeem attempts")

    body = await request.json()
    code_str = body.get("code", "").strip()
    if not code_str:
        raise HTTPException(status_code=400, detail="code is required")

    result = await db.execute(
        select(RedemptionCode)
        .where(RedemptionCode.code == code_str)
        .with_for_update()
    )
    code = result.scalar_one_or_none()
    if not code:
        raise HTTPException(status_code=404, detail="invalid redemption code")
    if code.status != "unused":
        raise HTTPException(status_code=400, detail="code already used or disabled")

    user = (
        await db.execute(select(User).where(User.id == cached_user.id).with_for_update())
    ).scalar_one()
    user.balance += code.balance_cents
    code.status = "used"
    code.used_by = user.id
    code.used_at = dt.utcnow()
    await ensure_finance_summary_initialized(db, user.id, commit=False)
    await increment_finance_summary(db, user.id, bonus_cents=code.balance_cents)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="concurrent redeem conflict, retry")

    return {
        "success": True,
        "added_cents": code.balance_cents,
        "new_balance": user.balance,
        "new_balance_usd": user.balance / 100,
        "message": "redemption successful",
    }


@router.get("/referral")
async def get_referral_info(request: Request, db: AsyncSession = Depends(get_db)):
    """查询邀请码、邀请记录和累计 API 额度奖励"""
    try:
        cached_user = await authenticate_user(request, db)
    except HTTPException as e:
        if e.status_code == 401:
            return openai_error("Invalid API key", "authentication_error", code="invalid_api_key", status_code=401)
        raise

    user = (await db.execute(select(User).where(User.id == cached_user.id))).scalar_one()

    if not user.referral_code:
        from .security import generate_referral_code
        user.referral_code = generate_referral_code()
        await db.commit()

    referred_users = (
        await db.execute(
            select(User)
            .where(User.referred_by == user.id)
            .order_by(User.created_at.desc())
            .limit(200)
        )
    ).scalars().all()
    referred_ids = [row.id for row in referred_users]

    total_reward = (await db.execute(
        select(func.coalesce(func.sum(ReferralReward.reward_cents), 0))
        .where(ReferralReward.referrer_id == user.id)
        .where((ReferralReward.recipient_id == user.id) | (ReferralReward.recipient_id.is_(None)))
    )).scalar() or 0

    referred_reward = 0
    rewards_by_referred = {referred_id: [] for referred_id in referred_ids}
    if referred_ids:
        all_rewards = (
            await db.execute(
                select(ReferralReward)
                .where(ReferralReward.referrer_id == user.id)
                .where(ReferralReward.referred_id.in_(referred_ids))
                .order_by(ReferralReward.created_at.desc())
            )
        ).scalars().all()
        for reward in all_rewards:
            rewards_by_referred.setdefault(reward.referred_id, []).append(reward)
            if getattr(reward, "recipient_id", None) == reward.referred_id:
                referred_reward += int(reward.reward_cents or 0)

    records = [build_referral_record(row, rewards_by_referred.get(row.id, [])) for row in referred_users]
    pending_count = sum(1 for row in records if row["next_step"] != "持续充值奖励中")

    return {
        "referral_code": user.referral_code,
        "invite_url_path": f"/register?ref={user.referral_code}",
        "invited_count": len(referred_users),
        "total_reward_cents": total_reward,
        "total_reward_usd": total_reward / 100,
        "friend_reward_cents": referred_reward,
        "friend_reward_usd": referred_reward / 100,
        "pending_count": pending_count,
        "commission_rate": settings.referral_commission_rate,
        "max_rewards_per_user": settings.referral_max_rewards_per_user,
        "reward_cap_usd": settings.referral_reward_cap_cents / 100,
        "signup_bonus_usd": settings.referral_signup_bonus_cents / 100,
        "signup_referrer_bonus_usd": settings.referral_signup_referrer_bonus_cents / 100,
        "first_usage_referrer_bonus_usd": settings.referral_first_usage_referrer_bonus_cents / 100,
        "new_user_bonus_usd": settings.referral_new_user_bonus_cents / 100,
        "records": records,
        "recent_rewards": [
            {
                "order_no": r.order_no,
                "reward_type": getattr(r, "reward_type", None) or REWARD_PURCHASE_COMMISSION,
                "recipient_id": getattr(r, "recipient_id", None) or r.referrer_id,
                "order_amount_cents": r.order_amount_cents,
                "reward_cents": r.reward_cents,
                "reward_usd": r.reward_cents / 100,
                "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
            }
            for rewards in rewards_by_referred.values()
            for r in rewards[:20]
        ],
    }


@router.patch("/referral/code")
async def update_referral_code(
    payload: ReferralCodeUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    try:
        cached_user = await authenticate_user(request, db)
    except HTTPException as e:
        if e.status_code == 401:
            return openai_error("Invalid API key", "authentication_error", code="invalid_api_key", status_code=401)
        raise

    next_code = payload.referral_code.strip().upper()
    reserved = {"ADMIN", "ROOT", "SUPPORT", "COINCOIN", "CLAWFATHER", "BIRDSYNC"}
    if next_code in reserved:
        raise HTTPException(status_code=400, detail="这个邀请码暂不可用")

    existing = (
        await db.execute(select(User).where(User.referral_code == next_code, User.id != cached_user.id))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="这个邀请码已经被使用")

    user = (await db.execute(select(User).where(User.id == cached_user.id))).scalar_one()
    user.referral_code = next_code
    await db.commit()
    return {"referral_code": user.referral_code, "invite_url_path": f"/register?ref={user.referral_code}"}


@router.get("/announcements")
async def list_announcements(db: AsyncSession = Depends(get_db)):
    from .models import Announcement
    from sqlalchemy import select

    result = await db.execute(
        select(Announcement)
        .where(Announcement.status == "active")
        .order_by(Announcement.created_at.desc())
        .limit(10)
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
            "created_at": a.created_at.isoformat() + "Z" if a.created_at else None,
        }
        for a in anns
    ]


@router.get("/models")
async def list_models(request: Request, db: AsyncSession = Depends(get_db)):
    """列出可用模型目录。"""
    station_models = None
    try:
        cached_user = await authenticate_user(request, db)
        from .stations import list_station_public_models_for_user
        station_models = await list_station_public_models_for_user(cached_user, db)
    except HTTPException:
        station_models = None
    except Exception:
        station_models = None

    user_agent = request.headers.get("user-agent", "")
    if user_agent.startswith("claude-cli"):
        models = []
        if station_models is not None:
            source_models = [model for model in station_models if "chat/completions" in (model.get("coincoin_capabilities") or [])]
            for model in source_models:
                models.append(
                    {
                        "type": "model",
                        "id": model["id"],
                        "display_name": model["id"],
                        "created_at": model.get("created", 1700000000),
                    }
                )
        else:
            for public_model in model_registry.list_public_models("chat/completions"):
                models.append(
                    {
                        "type": "model",
                        "id": public_model.public_id,
                        "display_name": public_model.public_id,
                        "created_at": public_model.created,
                    }
                )
        return {
            "data": models,
            "has_more": False,
            "first_id": models[0]["id"] if models else None,
            "last_id": models[-1]["id"] if models else None,
        }

    models = station_models if station_models is not None else [
        _serialize_public_model(public_model) for public_model in model_registry.list_public_models()
    ]
    return {
        "object": "list",
        "data": models,
    }


@router.get("/models/{model_id}")
async def get_model(model_id: str):
    """获取单个模型信息。"""
    public_model = model_registry.get_public_model(model_id)
    if public_model is None:
        return openai_error(f"Model '{model_id}' is not available", "invalid_request_error", param="model", code="model_not_found", status_code=404)
    return _serialize_public_model(public_model)


@router.get("/responses")
async def responses_alias_health():
    return await responses_health()


@router.post("/responses")
async def responses_alias(request: Request, db: AsyncSession = Depends(get_db)):
    return await proxy_responses(request, db)


@router.post("/images/generations")
async def images_generations(request: Request, db: AsyncSession = Depends(get_db)):
    return await proxy_images_generations(request, db)


@router.post("/images/edits")
async def images_edits(request: Request, db: AsyncSession = Depends(get_db)):
    return await proxy_images_edits(request, db)


@router.post("/chat/completions")
async def chat_completions(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        user = await authorize_request(request, db)
    except HTTPException as e:
        # 转换为 OpenAI 标准错误格式
        if e.status_code == 401:
            return openai_error("Invalid API key provided", "authentication_error", code="invalid_api_key", status_code=401)
        elif e.status_code == 403:
            return openai_error("Access denied", "permission_error", code="access_denied", status_code=403)
        elif e.status_code == 429:
            return openai_error(e.detail, "rate_limit_error", code="rate_limit_exceeded", status_code=429)
        raise

    try:
        payload = await request.json()
    except Exception:
        return openai_error("Invalid JSON payload", "invalid_request_error", code="invalid_json")

    if not isinstance(payload, dict):
        return openai_error("Request body must be a JSON object", "invalid_request_error")

    messages = payload.get("messages") or []
    if not isinstance(messages, list):
        return openai_error("'messages' must be an array", "invalid_request_error", param="messages")

    # ============== 处理 messages 格式兼容性 ==============
    # Azure Responses API 使用与 OpenAI Chat Completions 完全不同的消息格式
    # 
    # OpenAI Chat Completions 格式:
    #   {"role": "assistant", "content": null, "tool_calls": [...]}
    #   {"role": "tool", "tool_call_id": "xxx", "content": "result"}
    #
    # Azure Responses API 格式:
    #   {"type": "function_call", "call_id": "xxx", "name": "fn", "arguments": "{}"}
    #   {"type": "function_call_output", "call_id": "xxx", "output": "result"}
    #
    def convert_messages_for_responses_api(msgs: list) -> list:
        """将 OpenAI Chat Completions 消息格式转换为 Azure Responses API 格式"""
        converted = []
        for msg in msgs:
            if not isinstance(msg, dict):
                converted.append(msg)
                continue
            
            role = msg.get("role")
            
            # 1. 处理带 tool_calls 的 assistant 消息
            if role == "assistant" and "tool_calls" in msg:
                tool_calls = msg.get("tool_calls", [])
                content = msg.get("content")
                
                # 如果有文本内容，先添加 assistant 消息
                if content:
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                part["type"] = "output_text"
                    converted.append({"role": "assistant", "content": content})
                
                # 将每个 tool_call 转换为 function_call item
                # 兼容两种格式：
                # - 标准 OpenAI: {"id": "xxx", "type": "function", "function": {"name": "...", "arguments": "..."}}
                # - 简化格式 (nanobot): {"name": "read_file", "arguments_size": 83} 或 {"name": "...", "arguments": "..."}
                for idx, tc in enumerate(tool_calls):
                    # 标准格式：有 function 字段
                    if "function" in tc:
                        func = tc.get("function", {})
                        call_id = tc.get("id", f"call_{idx}")
                        name = func.get("name", "")
                        arguments = func.get("arguments", "{}")
                    else:
                        # 简化格式：name 直接在顶层
                        call_id = tc.get("id", f"call_{idx}")
                        name = tc.get("name", "")
                        arguments = tc.get("arguments", "{}")
                    
                    converted.append({
                        "type": "function_call",
                        "call_id": call_id,
                        "name": name,
                        "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False),
                    })
                continue
            
            # 2. 处理 tool 角色消息 (工具结果)
            if role == "tool":
                call_id = msg.get("tool_call_id", "")
                output = msg.get("content", "")
                converted.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output if output is not None else "",
                })
                continue
            
            # 3. 普通消息：处理 content 格式转换
            result = dict(msg)
            content = result.get("content")
            if content is None and "content" in result:
                result["content"] = ""
            elif isinstance(content, list):
                converted_parts = []
                for part in content:
                    if not isinstance(part, dict):
                        converted_parts.append(part)
                        continue
                    part = dict(part)
                    ptype = part.get("type")
                    if ptype == "text":
                        part["type"] = "output_text" if role == "assistant" else "input_text"
                    elif ptype == "image_url":
                        part["type"] = "input_image"
                        url_obj = part.pop("image_url", None)
                        if isinstance(url_obj, dict):
                            part["image_url"] = url_obj.get("url", "")
                        elif isinstance(url_obj, str):
                            part["image_url"] = url_obj
                    converted_parts.append(part)
                result["content"] = converted_parts
            converted.append(result)
        
        return converted

    converted_messages = convert_messages_for_responses_api(messages)

    requested_model = str(payload.get("model") or "").strip()
    tools = payload.get("tools") if isinstance(payload.get("tools"), list) else None
    try:
        station_model = await resolve_station_model_for_user(
            db,
            user,
            requested_model,
            "chat/completions",
            messages,
            tools,
        )
        resolved_model = station_model.resolved_model if station_model else model_registry.resolve_public_model(
            requested_model,
            "chat/completions",
            messages,
            tools,
        )
    except Exception as exc:
        return _model_resolution_to_openai_error(exc)
    public_model = resolved_model.public_model
    display_model = station_model.display_model if station_model else public_model.public_id
    used_cfg = resolved_model.backend
    used_route_reason = resolved_model.route_reason
    api_key_id = getattr(user, _KEY_ID_ATTR, "")
    price_input_per_million = station_model.retail_input_per_million if station_model else public_model.price_input_per_million
    price_output_per_million = station_model.retail_output_per_million if station_model else public_model.price_output_per_million

    if public_model.delivery_lane == gemini_cpa.DELIVERY_LANE:
        return await _proxy_gemini_cpa_chat_completions(
            payload=payload,
            user=user,
            public_model=public_model,
            display_model=display_model,
            used_cfg=used_cfg,
            used_route_reason=used_route_reason,
            api_key_id=api_key_id,
            price_input_per_million=price_input_per_million,
            price_output_per_million=price_output_per_million,
            station_model=station_model,
        )

    if public_model.delivery_lane == CLAUDE_COMPAT_PROVIDER_KIRO_GO:
        chat_payload: Dict[str, Any] = {
            "model": used_cfg.model_id,
            "messages": messages,
            "stream": bool(payload.get("stream")),
        }
        if "tools" in payload:
            tools_payload = _responses_tools_to_chat_tools(payload.get("tools"))
            if tools_payload:
                chat_payload["tools"] = tools_payload
        if "max_tokens" in payload:
            chat_payload["max_tokens"] = payload.get("max_tokens")
        if "max_completion_tokens" in payload and "max_tokens" not in chat_payload:
            chat_payload["max_tokens"] = payload.get("max_completion_tokens")
        for field in ("temperature", "top_p", "stop", "tool_choice"):
            if field in payload:
                chat_payload[field] = payload[field]
        prompt_cache_key = build_claude_code_prompt_cache_key(user, api_key_id, display_model, public_model)
        if prompt_cache_key:
            chat_payload["prompt_cache_key"] = prompt_cache_key

        headers = _build_upstream_headers(used_cfg)
        upstream_url = f"{_normalize_openai_base_url(used_cfg.upstream_url)}/chat/completions"
        if chat_payload.get("stream"):
            stream_client = await get_stream_client()
            try:
                req = stream_client.build_request("POST", upstream_url, json=chat_payload, headers=headers)
                upstream = await stream_client.send(req, stream=True)
            except (httpx.TimeoutException, httpx.RequestError):
                return openai_error("Upstream request failed", "server_error", code="upstream_unreachable", status_code=502)
            stream_headers = filter_headers(dict(upstream.headers))
            stream_headers.pop("content-length", None)
            stream_headers.setdefault("cache-control", "no-cache")
            stream_headers.setdefault("x-accel-buffering", "no")
            stream_headers["content-type"] = "text/event-stream; charset=utf-8"
            stream_t0 = time.monotonic()
            stream_usage = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}

            async def iter_events():
                finish_sent = False
                try:
                    async for line in upstream.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            event = json.loads(data_str)
                        except Exception:
                            continue
                        if not isinstance(event, dict):
                            continue
                        usage = event.get("usage")
                        if isinstance(usage, dict):
                            stream_usage["input"] = extract_total_input_tokens(usage)
                            stream_usage["output"] = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
                            stream_usage["cache_read"] = extract_cache_read_tokens(usage)
                            stream_usage["cache_creation"] = extract_cache_creation_tokens(usage)
                        if isinstance(event.get("error"), dict):
                            err = event["error"]
                            yield _chat_completion_chunk_line(
                                stream_id=str(event.get("id") or f"chatcmpl-{secrets.token_hex(12)}"),
                                display_model=display_model,
                                delta={},
                                finish_reason="stop",
                            )
                            yield f"data: {json.dumps({'error': err}, ensure_ascii=False)}\n\n"
                            yield "data: [DONE]\n\n"
                            finish_sent = True
                            return
                        if "model" in event:
                            event["model"] = display_model
                        yield f"{line.replace(used_cfg.model_id, display_model)}\n\n" if used_cfg.model_id and used_cfg.model_id in line else f"{line}\n\n"
                        choices = event.get("choices")
                        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
                        finish_reason = choice.get("finish_reason")
                        if finish_reason is not None:
                            finish_sent = True
                    if not finish_sent:
                        yield _chat_completion_chunk_line(
                            stream_id=f"chatcmpl-{secrets.token_hex(12)}",
                            display_model=display_model,
                            delta={},
                            finish_reason="stop",
                        )
                    yield "data: [DONE]\n\n"
                finally:
                    await upstream.aclose()
                    if upstream.status_code < 400:
                        dur = int((time.monotonic() - stream_t0) * 1000)
                        asyncio.create_task(usage_buffer.add(
                            user.id,
                            api_key_id=api_key_id,
                            input_tokens=stream_usage["input"],
                            output_tokens=stream_usage["output"],
                            cache_read_tokens=stream_usage["cache_read"],
                            cache_creation_tokens=stream_usage["cache_creation"],
                            requests=1,
                            endpoint="chat/completions:stream",
                            model=display_model,
                            customer_model_alias=display_model,
                            provider_model=public_model.provider_model or used_cfg.model_id,
                            route_reason=used_route_reason,
                            duration_ms=dur,
                            status_code=upstream.status_code,
                            price_input_per_million=price_input_per_million,
                            price_output_per_million=price_output_per_million,
                            usage_unit_type="tokens",
                            billable_sku=public_model.billable_sku or display_model,
                            upstream_request_id=extract_upstream_request_id(upstream.headers),
                            **usage_pricing_kwargs(public_model, station_model),
                        ))

            return StreamingResponse(
                iter_events(),
                status_code=upstream.status_code,
                headers=stream_headers,
                media_type="text/event-stream",
            )

        client = await get_http_client()
        t0 = time.monotonic()
        try:
            upstream = await client.post(upstream_url, json=chat_payload, headers=headers)
        except (httpx.TimeoutException, httpx.RequestError):
            return openai_error("Upstream request failed", "server_error", code="upstream_unreachable", status_code=502)
        duration_ms = int((time.monotonic() - t0) * 1000)
        response_headers = filter_headers(dict(upstream.headers))
        response_headers.pop("content-length", None)
        content_type = upstream.headers.get("content-type", "application/json")
        upstream_request_id = extract_upstream_request_id(upstream.headers)
        if "application/json" in content_type:
            try:
                data = upstream.json()
            except Exception:
                return openai_error("Upstream returned invalid JSON", "server_error", code="upstream_invalid_json", status_code=502)
        else:
            data = upstream.text

        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            return JSONResponse(content={"error": data["error"]}, status_code=upstream.status_code if upstream.status_code >= 400 else 502, headers=response_headers)
        if upstream.status_code >= 400:
            return JSONResponse(
                content={"error": {"message": str(data)[:500] if data else "upstream error", "type": "upstream_error", "code": str(upstream.status_code)}},
                status_code=upstream.status_code,
                headers=response_headers,
            )
        if not isinstance(data, dict):
            return Response(content=str(data), status_code=upstream.status_code, headers=response_headers, media_type=content_type)

        usage = _translate_chat_response_to_responses(data, display_model).get("usage") or {}
        await usage_buffer.add(
            user.id,
            api_key_id=api_key_id,
            input_tokens=extract_total_input_tokens(usage),
            output_tokens=int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
            cache_read_tokens=extract_cache_read_tokens(usage),
            cache_creation_tokens=extract_cache_creation_tokens(usage),
            requests=1,
            endpoint="chat/completions",
            model=display_model,
            customer_model_alias=display_model,
            provider_model=public_model.provider_model or used_cfg.model_id,
            route_reason=used_route_reason,
            duration_ms=duration_ms,
            status_code=upstream.status_code,
            price_input_per_million=price_input_per_million,
            price_output_per_million=price_output_per_million,
            usage_unit_type="tokens",
            billable_sku=public_model.billable_sku or display_model,
            upstream_request_id=upstream_request_id,
            **usage_pricing_kwargs(public_model, station_model),
        )

        data["model"] = display_model
        return JSONResponse(content=data, status_code=upstream.status_code, headers=response_headers)

    # ============== 构建 Responses API payload ==============
    resp_payload: Dict[str, Any] = {
        "model": used_cfg.model_id,
        "input": converted_messages,
        "stream": bool(payload.get("stream")),
    }
    prompt_cache_key = build_claude_code_prompt_cache_key(user, api_key_id, display_model, public_model)
    if prompt_cache_key:
        resp_payload["prompt_cache_key"] = prompt_cache_key

    if used_cfg.strip_unsupported:
        _sanitize_encrypted_ids(resp_payload)
    _ensure_content_text(resp_payload)

    # max_tokens -> max_output_tokens (will be stripped later if model doesn't support it)
    if "max_tokens" in payload:
        resp_payload["max_output_tokens"] = payload.get("max_tokens")
    if "max_completion_tokens" in payload:
        resp_payload["max_output_tokens"] = payload.get("max_completion_tokens")
    
    if not used_cfg.strip_unsupported:
        for field in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
            if field in payload:
                resp_payload[field] = payload[field]

    # 透传 stop（如果模型支持）
    if "stop" in payload:
        resp_payload["stop"] = payload["stop"]
    
    # seed 参数（某些模型支持）
    # if "seed" in payload:
    #     resp_payload["seed"] = payload["seed"]
    
    # response_format 支持 (JSON mode / Structured Outputs)
    if "response_format" in payload:
        rf = payload["response_format"]
        if isinstance(rf, dict):
            rf_type = rf.get("type")
            if rf_type == "json_object":
                # JSON mode
                resp_payload["text"] = {"format": {"type": "json_object"}}
            elif rf_type == "json_schema":
                # Structured Outputs
                resp_payload["text"] = {
                    "format": {
                        "type": "json_schema",
                        "json_schema": rf.get("json_schema", {}),
                    }
                }
            # text 类型是默认值，不需要特别处理
    
    # Tools/Functions 支持 - 需要转换格式
    # 兼容三种输入格式：
    # 1. 标准 OpenAI:  {"type":"function","function":{"name":"x","parameters":{...}}}
    # 2. Responses API: {"type":"function","name":"x","parameters":{...}}
    # 3. 简化格式 (nanobot): {"name":"read_file","params":["path"]}
    if "tools" in payload:
        converted_tools = []
        for tool in payload["tools"]:
            # 格式 1: 标准 OpenAI Chat Completions
            if tool.get("type") == "function" and "function" in tool:
                func = tool["function"]
                converted_tool = {
                    "type": "function",
                    "name": func.get("name"),
                    "description": func.get("description", ""),
                    "parameters": func.get("parameters", {}),
                }
                if "strict" in func:
                    converted_tool["strict"] = func["strict"]
                converted_tools.append(converted_tool)
            # 格式 2: 已经是 Responses API 格式
            elif tool.get("type") == "function" and "name" in tool:
                converted_tools.append(tool)
            # 格式 3: 简化格式 (nanobot) - {"name": "x", "params": [...]}
            elif "name" in tool and ("params" in tool or "parameters" not in tool):
                params = tool.get("params", [])
                # 将 params 数组转换为 JSON Schema 格式
                properties = {}
                if isinstance(params, list):
                    for p in params:
                        if isinstance(p, str):
                            properties[p] = {"type": "string"}
                        elif isinstance(p, dict):
                            properties.update(p)
                converted_tool = {
                    "type": "function",
                    "name": tool.get("name"),
                    "description": tool.get("description", ""),
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                    },
                }
                converted_tools.append(converted_tool)
            else:
                # 未知格式，尝试直接使用
                converted_tools.append(tool)
        resp_payload["tools"] = converted_tools
    
    for field in ("tool_choice", "parallel_tool_calls"):
        if field in payload:
            resp_payload[field] = payload[field]

    upstream_url = f"{used_cfg.upstream_url.rstrip('/')}/responses"

    def build_chat_response(resp: Dict, model_id: str) -> Dict:
        usage = resp.get("usage") or {}
        prompt_tokens = usage.get("input_tokens") or usage.get("prompt_tokens")
        completion_tokens = usage.get("output_tokens") or usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens") or (
            (prompt_tokens or 0) + (completion_tokens or 0)
        )

        text_chunks = []
        tool_calls = []
        finish_reason = "stop"

        for item in resp.get("output", []) or []:
            item_type = item.get("type")
            
            # 处理文本消息
            if item_type == "message":
                for c in item.get("content", []) or []:
                    if c.get("type") in ("output_text", "text") and c.get("text"):
                        text_chunks.append(c.get("text"))
            elif item_type in ("output_text", "text") and item.get("text"):
                text_chunks.append(item.get("text"))
            
            # 处理 function_call / tool_use (Responses API 格式)
            elif item_type in ("function_call", "tool_use", "function"):
                tool_call = {
                    "id": item.get("id") or item.get("call_id") or f"call_{secrets.token_hex(12)}",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or item.get("function", {}).get("name", ""),
                        "arguments": item.get("arguments") or item.get("function", {}).get("arguments", "{}"),
                    }
                }
                # arguments 可能是 dict，需要转成 string
                if isinstance(tool_call["function"]["arguments"], dict):
                    tool_call["function"]["arguments"] = json.dumps(tool_call["function"]["arguments"], ensure_ascii=False)
                tool_calls.append(tool_call)
                finish_reason = "tool_calls"

        if not text_chunks and resp.get("output_text"):
            text_chunks.append(resp.get("output_text"))
        content = "".join(text_chunks) if text_chunks else None

        # 构建 message
        message: Dict[str, object] = {"role": "assistant"}
        if content:
            message["content"] = content
        else:
            message["content"] = None
        if tool_calls:
            message["tool_calls"] = tool_calls

        usage_body = {
            "prompt_tokens": int(prompt_tokens or 0),
            "completion_tokens": int(completion_tokens or 0),
            "total_tokens": int(total_tokens or 0),
        }
        cache_read_tokens = extract_cache_read_tokens(usage)
        if cache_read_tokens or isinstance(usage.get("input_tokens_details"), dict) or isinstance(usage.get("prompt_tokens_details"), dict):
            usage_body["prompt_tokens_details"] = {"cached_tokens": cache_read_tokens}

        return {
            "id": resp.get("id") or f"chatcmpl-{secrets.token_hex(12)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_id,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": usage_body,
        }

    _STRIP_PARAMS = ("temperature", "top_p", "presence_penalty", "frequency_penalty",
                     "max_output_tokens", "n", "logprobs", "top_logprobs", "seed")

    if resp_payload.get("stream"):
        model_registry.ensure_initialized()
        fallback_cfg = model_registry.models.get("fallback") or model_registry.get("premium")
        cheap_cfg = model_registry.models.get("cheap")
        allow_fallback = public_model.routing_mode == "legacy_auto"
        is_cheap = bool(allow_fallback and cheap_cfg and used_cfg.model_id == cheap_cfg.model_id)
        can_fallback = allow_fallback and (
            (used_cfg.upstream_url != fallback_cfg.upstream_url) or (used_cfg.model_id != fallback_cfg.model_id)
        )
        if resolved_model.lock_model_selection and fallback_cfg.model_id != used_cfg.model_id:
            can_fallback = False
        stream_client = await get_stream_client()

        async def _send_stream(cfg):
            send_payload = dict(resp_payload)
            send_payload["model"] = cfg.model_id
            if cfg.strip_unsupported:
                for field in _STRIP_PARAMS:
                    send_payload.pop(field, None)
            else:
                for field in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
                    if field in payload:
                        send_payload[field] = payload[field]
            stream_upstream_url = f"{cfg.upstream_url.rstrip('/')}/responses"
            stream_headers = _build_upstream_headers(cfg)
            req = stream_client.build_request("POST", stream_upstream_url, json=send_payload, headers=stream_headers)
            return await stream_client.send(req, stream=True)

        try:
            upstream = await _send_stream(used_cfg)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            if can_fallback:
                _fb = "cheap" if is_cheap else "premium"
                used_cfg = fallback_cfg
                used_route_reason = f"{_fb}_fallback_timeout"
                can_fallback = False
                is_cheap = False
                upstream = await _send_stream(used_cfg)
            else:
                import logging as _logging
                _logging.getLogger("coincoin.compat").error("upstream stream connect error: %s", exc)
                return openai_error("Upstream request failed", "server_error", code="upstream_unreachable", status_code=502)

        if can_fallback and upstream.status_code >= 400:
            _fb = "cheap" if is_cheap else "premium"
            _code = upstream.status_code
            try:
                await upstream.aclose()
            except Exception:
                pass
            used_cfg = fallback_cfg
            used_route_reason = f"{_fb}_fallback_{_code}"
            can_fallback = False
            is_cheap = False
            upstream = await _send_stream(used_cfg)

        content_type = upstream.headers.get("content-type", "")
        if "text/event-stream" not in content_type:
            if can_fallback:
                try:
                    await upstream.aclose()
                except Exception:
                    pass
                _fb = "cheap" if is_cheap else "premium"
                used_cfg = fallback_cfg
                used_route_reason = f"{_fb}_fallback_unexpected"
                can_fallback = False
                is_cheap = False
                upstream = await _send_stream(used_cfg)
                content_type = upstream.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                try:
                    body = await upstream.aread()
                finally:
                    await upstream.aclose()
                response_headers = filter_headers(dict(upstream.headers))
                response_headers.pop("content-length", None)
                if upstream.status_code >= 400:
                    import logging
                    _logger = logging.getLogger("coincoin.compat")
                    _logger.error("upstream stream-fallback %s: %s", upstream.status_code, body[:1000])
                    if "application/json" in content_type:
                        try:
                            data = json.loads(body.decode("utf-8"))
                            if "error" in data:
                                return JSONResponse(content={"error": data["error"]}, status_code=upstream.status_code, headers=response_headers)
                        except Exception:
                            pass
                    return JSONResponse(
                        content={"error": {"message": body.decode("utf-8", errors="replace")[:500] or "upstream error", "type": "upstream_error", "code": str(upstream.status_code)}},
                        status_code=upstream.status_code, headers=response_headers,
                    )
                if "application/json" in content_type:
                    data = json.loads(body.decode("utf-8"))
                    if _responses_payload_is_empty_success(data):
                        return openai_error(
                            "Upstream completed without returning assistant text or tool calls",
                            "server_error",
                            code="upstream_empty_response",
                            status_code=502,
                        )
                    return JSONResponse(content=build_chat_response(data, display_model), status_code=upstream.status_code, headers=response_headers)
                return Response(content=body, status_code=upstream.status_code, headers=response_headers, media_type=content_type)

        stream_id = f"chatcmpl-{secrets.token_hex(12)}"
        tool_call_index = 0
        has_tool_calls = False
        first_content_sent = False
        compat_stream_t0 = time.monotonic()
        _compat_stream_usage = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}

        async def iter_events():
            nonlocal tool_call_index, has_tool_calls, first_content_sent
            finish_sent = False
            try:
                async for line in upstream.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except Exception:
                        continue
                    event_type = event.get("type")

                    usage = event.get("usage") or (event.get("response") or {}).get("usage")
                    if usage:
                        _compat_stream_usage["input"] = extract_total_input_tokens(usage)
                        _compat_stream_usage["output"] = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
                        _compat_stream_usage["cache_read"] = extract_cache_read_tokens(usage)
                        _compat_stream_usage["cache_creation"] = extract_cache_creation_tokens(usage)
                    
                    # 处理文本内容
                    if event_type in ("response.output_text.delta", "response.output_text.chunk"):
                        delta = event.get("delta")
                        if isinstance(delta, dict):
                            delta_text = delta.get("text")
                        else:
                            delta_text = delta if isinstance(delta, str) else None
                        if delta_text:
                            if not first_content_sent:
                                first_content_sent = True
                                delta_obj = {"role": "assistant", "content": delta_text}
                            else:
                                delta_obj = {"content": delta_text}
                            chunk = {
                                "id": stream_id,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": display_model,
                                "choices": [{"index": 0, "delta": delta_obj, "finish_reason": None}],
                            }
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    
                    # 处理 function_call 开始
                    elif event_type in ("response.function_call_arguments.start", "response.output_item.added"):
                        item = event.get("item", {})
                        if item.get("type") in ("function_call", "tool_use", "function"):
                            has_tool_calls = True
                            func_name = item.get("name") or item.get("function", {}).get("name", "")
                            call_id = item.get("id") or item.get("call_id") or f"call_{secrets.token_hex(12)}"
                            delta_obj: Dict[str, Any] = {
                                "tool_calls": [{
                                    "index": tool_call_index,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {"name": func_name, "arguments": ""}
                                }]
                            }
                            if not first_content_sent:
                                first_content_sent = True
                                delta_obj["role"] = "assistant"
                            chunk = {
                                "id": stream_id,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": display_model,
                                "choices": [{"index": 0, "delta": delta_obj, "finish_reason": None}],
                            }
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    
                    # 处理 function_call 参数增量
                    elif event_type == "response.function_call_arguments.delta":
                        delta_args = event.get("delta", "")
                        if delta_args:
                            chunk = {
                                "id": stream_id,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": display_model,
                                "choices": [{
                                    "index": 0,
                                    "delta": {
                                        "tool_calls": [{
                                            "index": tool_call_index,
                                            "function": {"arguments": delta_args}
                                        }]
                                    },
                                    "finish_reason": None
                                }],
                            }
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    
                    # 处理 function_call 完成
                    elif event_type == "response.function_call_arguments.done":
                        tool_call_index += 1
                    
                    # 处理上游错误事件
                    elif event_type in ("response.failed", "response.error", "error"):
                        error_info = event.get("error", {})
                        error_msg = error_info.get("message") if isinstance(error_info, dict) else str(error_info)
                        error_code = error_info.get("code") if isinstance(error_info, dict) else None
                        if not error_msg:
                            error_msg = event.get("message", "Unknown upstream error")
                        
                        error_data = {
                            "error": {
                                "message": error_msg,
                                "type": "server_error",
                                "code": error_code,
                            }
                        }
                        yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        return
                    
                    # 处理响应完成
                    if event_type in ("response.output_text.done", "response.completed"):
                        finish_reason = "tool_calls" if has_tool_calls else "stop"
                        finish = {
                            "id": stream_id,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": display_model,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                        }
                        if not finish_sent:
                            yield f"data: {json.dumps(finish)}\n\n"
                            finish_sent = True
                    if event_type == "response.completed":
                        break
                yield "data: [DONE]\n\n"
            except Exception:
                pass
            finally:
                await upstream.aclose()
                if upstream.status_code < 400:
                    dur = int((time.monotonic() - compat_stream_t0) * 1000)
                    asyncio.create_task(usage_buffer.add(
                        user.id,
                        api_key_id=api_key_id,
                        input_tokens=_compat_stream_usage["input"],
                        output_tokens=_compat_stream_usage["output"],
                        cache_read_tokens=_compat_stream_usage["cache_read"],
                        cache_creation_tokens=_compat_stream_usage["cache_creation"],
                        requests=1,
                        endpoint="chat/completions:stream",
                        model=display_model,
                        customer_model_alias=display_model,
                        provider_model=public_model.provider_model or used_cfg.model_id,
                        route_reason=used_route_reason,
                        duration_ms=dur,
                        status_code=upstream.status_code,
                        price_input_per_million=price_input_per_million,
                        price_output_per_million=price_output_per_million,
                        usage_unit_type="tokens",
                        billable_sku=public_model.billable_sku or display_model,
                        upstream_request_id=extract_upstream_request_id(upstream.headers),
                        **usage_pricing_kwargs(public_model, station_model),
                    ))
        stream_headers = filter_headers(dict(upstream.headers))
        stream_headers.pop("content-length", None)
        stream_headers.setdefault("cache-control", "no-cache")
        stream_headers.setdefault("x-accel-buffering", "no")
        return StreamingResponse(iter_events(), status_code=upstream.status_code, headers=stream_headers, media_type=content_type)

    model_registry.ensure_initialized()
    fallback_cfg = model_registry.models.get("fallback") or model_registry.get("premium")
    cheap_cfg = model_registry.models.get("cheap")
    allow_fallback = public_model.routing_mode == "legacy_auto"
    is_cheap = bool(allow_fallback and cheap_cfg and used_cfg.model_id == cheap_cfg.model_id)
    can_fallback = allow_fallback and (
        (used_cfg.upstream_url != fallback_cfg.upstream_url) or (used_cfg.model_id != fallback_cfg.model_id)
    )
    if resolved_model.lock_model_selection and fallback_cfg.model_id != used_cfg.model_id:
        can_fallback = False
    client = await get_http_client()

    async def _post_json(cfg):
        send_payload = dict(resp_payload)
        send_payload["model"] = cfg.model_id
        if cfg.strip_unsupported:
            for field in _STRIP_PARAMS:
                send_payload.pop(field, None)
        else:
            for field in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
                if field in payload:
                    send_payload[field] = payload[field]
        req_url = f"{cfg.upstream_url.rstrip('/')}/responses"
        req_headers = _build_upstream_headers(cfg)
        t0 = time.monotonic()
        r = await client.post(req_url, json=send_payload, headers=req_headers)
        dur = int((time.monotonic() - t0) * 1000)
        return r, dur

    try:
        upstream, duration_ms = await _post_json(used_cfg)
    except (httpx.TimeoutException, httpx.RequestError):
        if can_fallback:
            _fb = "cheap" if is_cheap else "premium"
            used_cfg = fallback_cfg
            used_route_reason = f"{_fb}_fallback_timeout"
            can_fallback = False
            is_cheap = False
            upstream, duration_ms = await _post_json(used_cfg)
        else:
            return openai_error("Upstream request failed", "server_error", code="upstream_unreachable", status_code=502)

    if can_fallback and upstream.status_code >= 400:
        _fb = "cheap" if is_cheap else "premium"
        used_cfg = fallback_cfg
        used_route_reason = f"{_fb}_fallback_{upstream.status_code}"
        can_fallback = False
        is_cheap = False
        upstream, duration_ms = await _post_json(used_cfg)
    response_headers = filter_headers(dict(upstream.headers))
    response_headers.pop("content-length", None)

    content_type = upstream.headers.get("content-type", "application/json")
    upstream_request_id = extract_upstream_request_id(upstream.headers)
    if can_fallback and "application/json" not in content_type:
        _fb = "cheap" if is_cheap else "premium"
        used_cfg = fallback_cfg
        used_route_reason = f"{_fb}_fallback_unexpected"
        can_fallback = False
        is_cheap = False
        upstream, duration_ms = await _post_json(used_cfg)
        response_headers = filter_headers(dict(upstream.headers))
        response_headers.pop("content-length", None)
        content_type = upstream.headers.get("content-type", "application/json")
        upstream_request_id = extract_upstream_request_id(upstream.headers)

    if "application/json" in content_type:
        try:
            data = upstream.json()
        except Exception:
            if can_fallback:
                _fb = "cheap" if is_cheap else "premium"
                used_cfg = fallback_cfg
                used_route_reason = f"{_fb}_fallback_unexpected"
                can_fallback = False
                is_cheap = False
                upstream, duration_ms = await _post_json(used_cfg)
                response_headers = filter_headers(dict(upstream.headers))
                response_headers.pop("content-length", None)
                content_type = upstream.headers.get("content-type", "application/json")
                upstream_request_id = extract_upstream_request_id(upstream.headers)
                data = upstream.json() if "application/json" in content_type else upstream.text
            else:
                return openai_error("Upstream returned invalid JSON", "server_error", code="upstream_invalid_json", status_code=502)
    else:
        data = upstream.text

    input_tokens_delta = 0
    output_tokens_delta = 0
    cache_read_tokens_delta = 0
    cache_creation_tokens_delta = 0
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        return JSONResponse(
            content={"error": data["error"]},
            status_code=upstream.status_code if upstream.status_code >= 400 else 502,
            headers=response_headers,
        )
    if upstream.status_code < 400 and isinstance(data, dict):
        if _responses_payload_is_empty_success(data):
            return openai_error(
                "Upstream completed without returning assistant text or tool calls",
                "server_error",
                code="upstream_empty_response",
                status_code=502,
            )
        usage = data.get("usage") or {}
        input_tokens_delta = extract_total_input_tokens(usage)
        output_tokens_delta = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        cache_read_tokens_delta = extract_cache_read_tokens(usage)
        cache_creation_tokens_delta = extract_cache_creation_tokens(usage)

    if upstream.status_code < 400:
        await usage_buffer.add(
            user.id,
            api_key_id=api_key_id,
            input_tokens=input_tokens_delta,
            output_tokens=output_tokens_delta,
            cache_read_tokens=cache_read_tokens_delta,
            cache_creation_tokens=cache_creation_tokens_delta,
            requests=1,
            endpoint="chat/completions",
            model=display_model,
            customer_model_alias=display_model,
            provider_model=public_model.provider_model or used_cfg.model_id,
            route_reason=used_route_reason,
            duration_ms=duration_ms,
            status_code=upstream.status_code,
            price_input_per_million=price_input_per_million,
            price_output_per_million=price_output_per_million,
            usage_unit_type="tokens",
            billable_sku=public_model.billable_sku or display_model,
            upstream_request_id=upstream_request_id,
            **usage_pricing_kwargs(public_model, station_model),
        )
    else:
        import logging
        _logger = logging.getLogger("coincoin.compat")
        _logger.error("upstream %s for chat/completions: %s", upstream.status_code, str(data)[:1000])

    if upstream.status_code >= 400:
        if isinstance(data, dict) and "error" in data:
            return JSONResponse(content={"error": data["error"]}, status_code=upstream.status_code, headers=response_headers)
        return JSONResponse(
            content={"error": {"message": str(data)[:500] if data else "upstream error", "type": "upstream_error", "code": str(upstream.status_code)}},
            status_code=upstream.status_code, headers=response_headers,
        )

    if isinstance(data, dict):
        return JSONResponse(content=build_chat_response(data, display_model), status_code=upstream.status_code, headers=response_headers)

    return Response(content=str(data), status_code=upstream.status_code, headers=response_headers, media_type=content_type)


async def _proxy_gemini_cpa_chat_completions(
    *,
    payload: Dict[str, Any],
    user,
    public_model,
    display_model: str,
    used_cfg,
    used_route_reason: str,
    api_key_id: str,
    price_input_per_million: int,
    price_output_per_million: int,
    station_model,
):
    try:
        channel = gemini_cpa.select_channel(public_model, used_cfg)
    except gemini_cpa.GeminiCpaChannelUnavailable as exc:
        return openai_error(str(exc), "server_error", code="gemini_cpa_channel_cooling_down", status_code=503)

    send_payload = dict(payload)
    send_payload["model"] = channel.provider_model
    send_payload.pop("model_provider", None)
    headers = gemini_cpa.build_headers(channel)
    upstream_url = gemini_cpa.chat_completions_url(channel)

    client = await get_http_client()
    t0 = time.monotonic()
    try:
        upstream = await client.post(upstream_url, json=send_payload, headers=headers)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        gemini_cpa.record_failure(channel)
        import logging
        logging.getLogger("coincoin.compat").error("gemini cpa chat transport error: %s", exc)
        return openai_error("Upstream request failed", "server_error", code="upstream_unreachable", status_code=502)
    duration_ms = int((time.monotonic() - t0) * 1000)

    response_headers = filter_headers(dict(upstream.headers))
    response_headers.pop("content-length", None)
    for header_name, header_value in gemini_cpa.iter_channel_debug_headers(channel):
        response_headers.setdefault(header_name, header_value)
    content_type = upstream.headers.get("content-type", "application/json")
    upstream_request_id = extract_upstream_request_id(upstream.headers)

    if "application/json" in content_type:
        try:
            data = upstream.json()
        except Exception:
            gemini_cpa.record_failure(channel)
            return openai_error("Upstream returned invalid JSON", "server_error", code="upstream_invalid_json", status_code=502)
    else:
        data = upstream.text

    if upstream.status_code < 400:
        gemini_cpa.record_success(channel)
    elif gemini_cpa.should_record_failure(upstream.status_code):
        gemini_cpa.record_failure(channel)

    input_tokens_delta = 0
    output_tokens_delta = 0
    cache_read_tokens_delta = 0
    cache_creation_tokens_delta = 0
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        return JSONResponse(
            content={"error": data["error"]},
            status_code=upstream.status_code if upstream.status_code >= 400 else 502,
            headers=response_headers,
        )
    if upstream.status_code < 400 and isinstance(data, dict):
        usage = data.get("usage") or {}
        input_tokens_delta = extract_total_input_tokens(usage)
        output_tokens_delta = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        cache_read_tokens_delta = extract_cache_read_tokens(usage)
        cache_creation_tokens_delta = extract_cache_creation_tokens(usage)

        await usage_buffer.add(
            user.id,
            api_key_id=api_key_id,
            input_tokens=input_tokens_delta,
            output_tokens=output_tokens_delta,
            cache_read_tokens=cache_read_tokens_delta,
            cache_creation_tokens=cache_creation_tokens_delta,
            requests=1,
            endpoint="chat/completions",
            model=display_model,
            customer_model_alias=display_model,
            provider_model=public_model.provider_model or channel.provider_model,
            route_reason=used_route_reason,
            duration_ms=duration_ms,
            status_code=upstream.status_code,
            price_input_per_million=price_input_per_million,
            price_output_per_million=price_output_per_million,
            usage_unit_type="tokens",
            billable_sku=public_model.billable_sku or display_model,
            upstream_request_id=upstream_request_id,
            **usage_pricing_kwargs(public_model, station_model),
        )

    if upstream.status_code >= 400:
        if isinstance(data, dict) and "error" in data:
            return JSONResponse(content={"error": data["error"]}, status_code=upstream.status_code, headers=response_headers)
        return JSONResponse(
            content={"error": {"message": str(data)[:500] if data else "upstream error", "type": "upstream_error", "code": str(upstream.status_code)}},
            status_code=upstream.status_code,
            headers=response_headers,
        )

    if isinstance(data, dict):
        data["model"] = display_model
        return JSONResponse(content=data, status_code=upstream.status_code, headers=response_headers)

    return Response(content=str(data), status_code=upstream.status_code, headers=response_headers, media_type=content_type)


@router.post("/embeddings")
async def embeddings(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        user = await authorize_request(request, db)
    except HTTPException as e:
        if e.status_code == 401:
            return openai_error("Invalid API key provided", "authentication_error", code="invalid_api_key", status_code=401)
        elif e.status_code == 403:
            return openai_error("Access denied", "permission_error", code="access_denied", status_code=403)
        elif e.status_code == 429:
            return openai_error(e.detail, "rate_limit_error", code="rate_limit_exceeded", status_code=429)
        raise

    try:
        payload = await request.json()
    except Exception:
        return openai_error("Invalid JSON payload", "invalid_request_error", code="invalid_json")

    if not isinstance(payload, dict):
        return openai_error("Request body must be a JSON object", "invalid_request_error")

    requested_model = str(payload.get("model") or "").strip()
    try:
        station_model = await resolve_station_model_for_user(db, user, requested_model, "embeddings")
        resolved_model = station_model.resolved_model if station_model else model_registry.resolve_public_model(requested_model, "embeddings")
    except Exception as exc:
        return _model_resolution_to_openai_error(exc)

    public_model = resolved_model.public_model
    display_model = station_model.display_model if station_model else public_model.public_id
    used_cfg = resolved_model.backend
    used_route_reason = resolved_model.route_reason
    price_input_per_million = station_model.retail_input_per_million if station_model else public_model.price_input_per_million
    price_output_per_million = station_model.retail_output_per_million if station_model else public_model.price_output_per_million

    payload["model"] = used_cfg.model_id
    payload.pop("model_provider", None)

    upstream_url = f"{used_cfg.upstream_url.rstrip('/')}/embeddings"
    headers = _build_upstream_headers(used_cfg)

    client = await get_http_client()
    t0 = time.monotonic()
    upstream = await client.post(upstream_url, json=payload, headers=headers)
    duration_ms = int((time.monotonic() - t0) * 1000)
    response_headers = filter_headers(dict(upstream.headers))
    response_headers.pop("content-length", None)
    upstream_request_id = extract_upstream_request_id(upstream.headers)

    content_type = upstream.headers.get("content-type", "application/json")
    if "application/json" in content_type:
        try:
            data = upstream.json()
        except Exception:
            return openai_error("Upstream returned invalid JSON", "server_error", code="upstream_invalid_json", status_code=502)
    else:
        data = upstream.text

    input_tokens_delta = 0
    cache_read_tokens_delta = 0
    cache_creation_tokens_delta = 0
    if upstream.status_code < 400 and isinstance(data, dict):
        usage = data.get("usage") or {}
        input_tokens_delta = int(usage.get("prompt_tokens") or usage.get("total_tokens") or 0)
        cache_read_tokens_delta = extract_cache_read_tokens(usage)
        cache_creation_tokens_delta = extract_cache_creation_tokens(usage)

    if upstream.status_code < 400:
        await usage_buffer.add(
            user.id,
            api_key_id=getattr(user, _KEY_ID_ATTR, ""),
            input_tokens=input_tokens_delta,
            output_tokens=0,
            cache_read_tokens=cache_read_tokens_delta,
            cache_creation_tokens=cache_creation_tokens_delta,
            requests=1,
            endpoint="embeddings",
            model=display_model,
            customer_model_alias=display_model,
            provider_model=public_model.provider_model or used_cfg.model_id,
            route_reason=used_route_reason,
            duration_ms=duration_ms,
            status_code=upstream.status_code,
            price_input_per_million=price_input_per_million,
            price_output_per_million=price_output_per_million,
            usage_unit_type="tokens",
            billable_sku=public_model.billable_sku or display_model,
            upstream_request_id=upstream_request_id,
            **usage_pricing_kwargs(public_model, station_model),
        )

    if isinstance(data, dict):
        return JSONResponse(content=data, status_code=upstream.status_code, headers=response_headers)

    return Response(content=str(data), status_code=upstream.status_code, headers=response_headers, media_type=content_type)
