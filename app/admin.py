import logging
import os
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import gemini_cpa
from .db import get_db
from .epay import EpayVerificationError, epay_configured, extract_epay_params_from_proof_url, verify_epay_callback_params
from .finance_summary import (
    build_user_finance_snapshot,
    build_user_finance_snapshots,
    ensure_finance_summary_initialized,
    increment_finance_summary,
)
from .models import (
    Announcement,
    ApiKey,
    BillingLedgerEntry,
    CreditBalance,
    ModelAliasOverride,
    ModelChannelRoute,
    ModelPricingOverride,
    ProviderChannel,
    ProviderChannelMonitor,
    ProviderChannelMonitorDailyRollup,
    ProviderChannelMonitorHistory,
    ProviderChannelRuntimeState,
    SystemSetting,
    PaymentOrder,
    RechargeLog,
    RedemptionCode,
    RedemptionCodeUse,
    ReferralReward,
    RequestLog,
    Station,
    StationCustomerLink,
    TrafficPackBalance,
    UsageDaily,
    User,
    Account,
    UserFinanceSummary,
    UserModelPricingOverride,
    UserModelRoutingOverride,
    UserSubscription,
)
from .model_alias_overrides import (
    apply_runtime_alias_override,
    clear_runtime_alias_override,
    refresh_model_alias_registry_from_db,
)
from .payment_common import PaymentConfirmError, confirm_paid_order
from .schemas import (
    AdminKeyUpdate, AdminPaymentManualConfirmRequest, AdminSubscriptionAdjustRequest,
    AdminTrafficPackGrantRequest, AdminTrafficPackUpdateRequest, AdminUserPasswordResetRequest,
    AdminUserPasswordResetResponse, AdminUserCreateRequest, AdminUserCreateResponse, AdminUserUpdate,
    AdminClaudeCompatSettingsUpdate, AdminModelAliasUpdate, AdminModelChannelRouteCreate,
    AdminModelChannelRouteUpdate, AdminModelPricingUpdate, AdminProviderChannelCreate,
    AdminProviderChannelMonitorCreate, AdminProviderChannelMonitorSelectionUpdate,
    AdminProviderChannelMonitorUpdate,
    AdminProviderChannelUpdate, AdminUserModelPricingOverrideUpsert,
    AdminUserModelRoutingOverrideUpsert, AnnouncementCreate, AnnouncementUpdate,
    RedemptionCodeUpdateRequest, RedemptionGenerateRequest, RedemptionGenerateResponse,
)
from .channel_monitoring import (
    AUTO_MONITOR_CREATED_BY,
    MANUAL_MONITOR_CREATED_BY,
    desired_route_monitor_specs,
    monitor_availability_rows,
    monitor_model_list,
    parse_monitor_models,
    reconcile_provider_channel_monitors,
    run_provider_channel_monitor_once,
    serialize_monitor_models,
)
from .channel_monitor_leases import (
    ProviderChannelMonitorClaimedError,
    claim_provider_channel_monitor_for_run,
)
from .reliability import invalidate_reliability_cache
from .model_pricing_overrides import refresh_model_pricing_registry_from_db
from .system_settings import (
    CLAUDE_COMPAT_PROVIDER_KEY,
    apply_runtime_system_setting,
    refresh_runtime_system_settings_from_db,
)
from .config import settings as _settings
from .billing import (
    ADDONS_BY_ID,
    MONTHLY_BY_ID,
    TRAFFIC_PACK_VALID_DAYS,
    add_billing_ledger,
    available_subscription_cents,
    get_available_balance_cents,
    get_subscription_for_update,
    get_traffic_pack_for_update,
    normalize_subscription_period,
    product_by_id,
    serialize_billing_state,
    utcnow,
)
from .router import registry as model_registry
from .channel_router import channel_router
from .provider_channels import refresh_provider_channel_router_from_db
from .router import (
    CLAUDE_COMPAT_PROVIDER_KIRO_GO,
    CLAUDE_COMPAT_PROVIDER_UPSTREAM_DIRECT,
    CLAUDE_COMPAT_PROVIDERS,
)
from .security import decrypt_api_key, encrypt_api_key, generate_api_key, generate_id, generate_referral_code, hash_key, hash_password, require_admin


router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger("coincoin.admin")
ADMIN_UPLOAD_ROOT = Path(_settings.admin_upload_dir)
ANALYTICS_BALANCE_CACHE_TTL_SECONDS = 60
_analytics_balance_cache: dict[str, tuple[float, int]] = {}
ANALYTICS_DASHBOARD_CACHE_TTL_SECONDS = 60
_analytics_dashboard_cache: dict[str, tuple[float, dict]] = {}


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


def _product_admin_payload(product_id: str) -> dict:
    product = product_by_id(product_id)
    if not product:
        return {
            "product_id": product_id or "",
            "product_name": product_id or "",
            "product_kind": "legacy",
        }
    return {
        "product_id": product.id,
        "product_name": product.name,
        "product_kind": product.kind,
        "product_money": product.money,
        "product_balance_cents": product.balance_cents,
        "product_rank": product.rank,
        "product_min_plan_rank": product.min_plan_rank,
    }


def _normalize_utc_naive(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _money_text_to_minor_cents(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(round(float(text) * 100))
    except (TypeError, ValueError):
        return 0


def _parse_admin_date(value: Optional[str], *, end_of_day: bool = False) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed_date = date.fromisoformat(text)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid date filter")
        parsed = datetime.combine(parsed_date, datetime.max.time() if end_of_day else datetime.min.time())
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


async def _admin_billing_state(db: AsyncSession, user: User) -> dict:
    snapshot = await get_available_balance_cents(db, user)
    all_packs = list((
        await db.execute(
            select(TrafficPackBalance)
            .where(TrafficPackBalance.user_id == user.id)
            .order_by(TrafficPackBalance.created_at.desc())
            .limit(50)
        )
    ).scalars().all())
    active_ids = {getattr(pack, "id", "") for pack in (snapshot.get("traffic_packs") or [])}
    merged_packs = list(snapshot.get("traffic_packs") or [])
    merged_packs.extend([pack for pack in all_packs if getattr(pack, "id", "") not in active_ids])
    return serialize_billing_state(
        snapshot.get("subscription"),
        merged_packs,
        user,
        credit_cents=snapshot.get("credit_cents", 0),
    )


async def _admin_billing_states_batch(db: AsyncSession, users: list[User]) -> dict[str, dict]:
    if not users:
        return {}

    user_ids = [user.id for user in users]
    current = utcnow()
    subscriptions = (
        await db.execute(
            select(UserSubscription).where(UserSubscription.user_id.in_(user_ids))
        )
    ).scalars().all()
    active_packs = (
        await db.execute(
            select(TrafficPackBalance)
            .where(
                TrafficPackBalance.user_id.in_(user_ids),
                TrafficPackBalance.status == "active",
                TrafficPackBalance.remaining_cents > 0,
                TrafficPackBalance.expires_at > current,
            )
            .order_by(
                TrafficPackBalance.user_id.asc(),
                TrafficPackBalance.expires_at.asc(),
                TrafficPackBalance.created_at.asc(),
                TrafficPackBalance.id.asc(),
            )
        )
    ).scalars().all()
    ranked_pack_ids = (
        select(
            TrafficPackBalance.id.label("pack_id"),
            func.row_number().over(
                partition_by=TrafficPackBalance.user_id,
                order_by=(
                    TrafficPackBalance.created_at.desc(),
                    TrafficPackBalance.id.desc(),
                ),
            ).label("pack_rank"),
        )
        .where(TrafficPackBalance.user_id.in_(user_ids))
        .subquery()
    )
    recent_packs = (
        await db.execute(
            select(TrafficPackBalance)
            .join(ranked_pack_ids, TrafficPackBalance.id == ranked_pack_ids.c.pack_id)
            .where(ranked_pack_ids.c.pack_rank <= 50)
            .order_by(
                TrafficPackBalance.user_id.asc(),
                TrafficPackBalance.created_at.desc(),
                TrafficPackBalance.id.desc(),
            )
        )
    ).scalars().all()
    credit_batches = (
        await db.execute(
            select(CreditBalance).where(
                CreditBalance.user_id.in_(user_ids),
                CreditBalance.status == "active",
                CreditBalance.remaining_cents > 0,
            )
        )
    ).scalars().all()

    for subscription in subscriptions:
        normalize_subscription_period(subscription, current)

    subscriptions_by_user = {subscription.user_id: subscription for subscription in subscriptions}
    packs_by_user: dict[str, list[TrafficPackBalance]] = {user_id: [] for user_id in user_ids}
    active_pack_ids_by_user: dict[str, set[str]] = {user_id: set() for user_id in user_ids}
    for pack in active_packs:
        packs_by_user.setdefault(pack.user_id, []).append(pack)
        active_pack_ids_by_user.setdefault(pack.user_id, set()).add(pack.id)
    for pack in recent_packs:
        if pack.id not in active_pack_ids_by_user.setdefault(pack.user_id, set()):
            packs_by_user.setdefault(pack.user_id, []).append(pack)
    credit_cents_by_user = {user_id: 0 for user_id in user_ids}
    for batch in credit_batches:
        credit_cents_by_user[batch.user_id] = (
            credit_cents_by_user.get(batch.user_id, 0)
            + max(0, int(batch.remaining_cents or 0))
        )

    return {
        user.id: serialize_billing_state(
            subscriptions_by_user.get(user.id),
            packs_by_user.get(user.id, []),
            user,
            now=current,
            credit_cents=credit_cents_by_user.get(user.id, 0),
        )
        for user in users
    }


def _billing_summary_for_admin(billing: dict) -> dict:
    subscription = billing.get("subscription") or {}
    traffic_packs = billing.get("traffic_packs") or {}
    legacy_balance = billing.get("legacy_balance") or {}
    credit_wallet = billing.get("credit_wallet") or billing.get("credit_balance") or {}
    available = billing.get("available") or {}
    return {
        "available_cents": int(available.get("remaining_cents") or 0),
        "available_usd": float(available.get("remaining_usd") or 0),
        "subscription_active": bool(subscription.get("active")),
        "subscription_plan_id": subscription.get("plan_id"),
        "subscription_plan_name": subscription.get("plan_name"),
        "subscription_remaining_cents": int(subscription.get("remaining_cents") or 0),
        "subscription_quota_cents": int(subscription.get("quota_cents") or 0),
        "subscription_used_cents": int(subscription.get("used_cents") or 0),
        "subscription_period_end": subscription.get("period_end"),
        "subscription_paid_until": subscription.get("paid_until"),
        "traffic_pack_remaining_cents": int(traffic_packs.get("remaining_cents") or 0),
        "traffic_pack_count": len(traffic_packs.get("items") or []),
        "credit_wallet_cents": int(credit_wallet.get("remaining_cents") or 0),
        "credit_wallet_usd": float(credit_wallet.get("remaining_usd") or 0),
        "legacy_balance_cents": int(legacy_balance.get("remaining_cents") or 0),
    }


def _admin_available_fields(billing: dict) -> dict:
    credit_wallet = billing.get("credit_wallet") or billing.get("credit_balance") or {}
    available = billing.get("available") or {}
    return {
        "credit_wallet_cents": int(credit_wallet.get("remaining_cents") or 0),
        "credit_wallet_usd": float(credit_wallet.get("remaining_usd") or 0),
        "available_cents": int(available.get("remaining_cents") or 0),
        "available_usd": float(available.get("remaining_usd") or 0),
    }


def _admin_finance_summary(finance: dict, billing: dict) -> dict:
    payload = dict(finance or {})
    fields = _admin_available_fields(billing)
    payload.update({
        "current_credit_wallet_cents": fields["credit_wallet_cents"],
        "current_credit_wallet_usd": fields["credit_wallet_usd"],
        "current_available_cents": fields["available_cents"],
        "current_available_usd": fields["available_usd"],
    })
    return payload


def _alias_payload(alias_id: str):
    alias = model_registry.get_admin_alias(alias_id)
    if not alias:
        return None
    return {
        "alias": alias,
        "targets": model_registry.candidate_alias_targets(alias_id),
    }


def _matching_target(alias_id: str, target_alias: str):
    for candidate in model_registry.candidate_alias_targets(alias_id):
        if candidate["id"] == target_alias:
            return candidate
    return None


def _matching_target_by_models(alias_id: str, provider_model: str, upstream_model: str):
    for candidate in model_registry.candidate_alias_targets(alias_id):
        candidate_provider = candidate.get("provider_model") or ""
        candidate_upstream = candidate.get("upstream_model") or candidate_provider
        if candidate_provider == provider_model and candidate_upstream == upstream_model:
            return candidate
    return None


def _pricing_payload(model_id: str):
    model = model_registry.get_public_model(model_id)
    if not model:
        return None
    return {
        "id": model.public_id,
        "owned_by": getattr(model, "owned_by", ""),
        "provider_name": getattr(model, "provider_name", ""),
        "provider_model": getattr(model, "provider_model", ""),
        "delivery_lane": getattr(model, "delivery_lane", ""),
        "capabilities": list(getattr(model, "capabilities", ()) or ()),
        "billable_sku": getattr(model, "billable_sku", ""),
        "base_price_input_per_million": getattr(model, "base_price_input_per_million", 0),
        "base_price_output_per_million": getattr(model, "base_price_output_per_million", 0),
        "base_price_per_image_cents": getattr(model, "base_price_per_image_cents", 0.0),
        "base_price_per_video_cents": getattr(model, "base_price_per_video_cents", 0.0),
        "price_input_per_million": getattr(model, "price_input_per_million", 0),
        "price_output_per_million": getattr(model, "price_output_per_million", 0),
        "price_per_image_cents": getattr(model, "price_per_image_cents", 0.0),
        "price_per_video_cents": getattr(model, "price_per_video_cents", 0.0),
        "effective_cached_input_per_million": getattr(model, "effective_cached_input_per_million", 0.0),
        "pricing_mode": getattr(model, "pricing_mode", "explicit_price"),
        "model_multiplier": getattr(model, "model_multiplier", 1.0),
        "output_multiplier": getattr(model, "output_multiplier", 1.0),
        "cache_read_multiplier": getattr(model, "cache_read_multiplier", 0.0),
        "image_multiplier": getattr(model, "image_multiplier", 1.0),
        "video_multiplier": getattr(model, "video_multiplier", 1.0),
        "price_version": getattr(model, "price_version", 0),
        "override_active": model.public_id in model_registry.pricing_overrides,
    }


def _serialize_user_routing_override(row: Any) -> dict:
    public_model_id = str(getattr(row, "public_model_id", "") or "").strip()
    return {
        "public_model_id": public_model_id,
        "provider_model": str(getattr(row, "provider_model", "") or "").strip(),
        "upstream_model": str(getattr(row, "upstream_model", "") or "").strip(),
        "enabled": bool(getattr(row, "enabled", 1)),
        "updated_by": str(getattr(row, "updated_by", "") or "").strip(),
        "updated_at": getattr(row, "updated_at", None),
        "targets": model_registry.candidate_alias_targets(public_model_id),
    }


def _user_routing_override_payload(
    *,
    public_model_id: str,
    provider_model: str,
    upstream_model: str,
    enabled: bool,
    updated_by: str,
    updated_at: Any = None,
) -> dict:
    public_model_id = str(public_model_id or "").strip()
    return {
        "public_model_id": public_model_id,
        "provider_model": str(provider_model or "").strip(),
        "upstream_model": str(upstream_model or "").strip(),
        "enabled": bool(enabled),
        "updated_by": str(updated_by or "").strip(),
        "updated_at": updated_at,
        "targets": model_registry.candidate_alias_targets(public_model_id),
    }


def _serialize_user_pricing_override(row: Any) -> dict:
    return {
        "public_model_id": str(getattr(row, "public_model_id", "") or "").strip(),
        "cache_read_multiplier_override": (
            None
            if getattr(row, "cache_read_multiplier_override", None) is None
            else float(getattr(row, "cache_read_multiplier_override"))
        ),
        "updated_by": str(getattr(row, "updated_by", "") or "").strip(),
        "updated_at": getattr(row, "updated_at", None),
    }


def _user_pricing_override_payload(
    *,
    public_model_id: str,
    cache_read_multiplier_override: Optional[float],
    updated_by: str,
    updated_at: Any = None,
) -> dict:
    return {
        "public_model_id": str(public_model_id or "").strip(),
        "cache_read_multiplier_override": (
            None if cache_read_multiplier_override is None else float(cache_read_multiplier_override)
        ),
        "updated_by": str(updated_by or "").strip(),
        "updated_at": updated_at,
    }


def _user_override_model_options() -> list[dict]:
    model_registry.ensure_initialized()
    return [
        {
            "id": model.public_id,
            "provider_name": model.provider_name,
            "delivery_lane": model.delivery_lane,
            "capabilities": list(model.capabilities or ()),
            "targets": model_registry.candidate_alias_targets(model.public_id),
        }
        for model in model_registry.list_public_models()
        if str(model.public_id or "").strip()
    ]


async def _invalidate_user_key_cache(db: AsyncSession, user_id: str) -> None:
    try:
        keys = (
            await db.execute(select(ApiKey).where(ApiKey.user_id == user_id))
        ).scalars().all()
        from .proxy import key_cache

        for key in keys:
            await key_cache.delete(getattr(key, "key_hash", ""))
    except Exception:
        pass


def _csv_from_list(items: Optional[list[str]]) -> str:
    return ",".join(str(item).strip() for item in (items or []) if str(item).strip())


def _list_from_csv(raw: Optional[str]) -> list[str]:
    return [item.strip() for item in str(raw or "").replace("\n", ",").split(",") if item.strip()]


def _provider_channel_key_fingerprint(row: ProviderChannel) -> str:
    raw = _recover_raw_key(getattr(row, "encrypted_api_key", None))
    return _key_fingerprint(hash_key(raw)) if raw else ""


def _channel_runtime_payload(channel_id: str, db_state: Optional[ProviderChannelRuntimeState] = None) -> dict:
    state = channel_router.channel_state(channel_id)
    cooldown_until = float(state.get("cooldown_until") or 0)
    now = time.time()
    payload = {
        "memory_failures": int(state.get("failures") or 0),
        "memory_cooldown_until": cooldown_until,
        "memory_cooldown_remaining_seconds": max(0, int(cooldown_until - now)) if cooldown_until else 0,
        "memory_last_error_code": state.get("last_error_code", ""),
        "memory_rolling_latency_ms": int(state.get("rolling_latency_ms") or 0),
    }
    if db_state is not None:
        payload.update({
            "db_fail_count": int(getattr(db_state, "fail_count", 0) or 0),
            "db_cooldown_until": getattr(db_state, "cooldown_until", None),
            "db_last_success_at": getattr(db_state, "last_success_at", None),
            "db_last_failure_at": getattr(db_state, "last_failure_at", None),
            "db_last_error_code": getattr(db_state, "last_error_code", "") or "",
            "db_rolling_latency_ms": int(getattr(db_state, "rolling_latency_ms", 0) or 0),
        })
    return payload


def _provider_channel_payload(
    row: ProviderChannel,
    *,
    route_count: int = 0,
    runtime_state: Optional[ProviderChannelRuntimeState] = None,
    billing_stats: Optional[dict[str, int]] = None,
    monitor_selection: Optional[dict[str, Any]] = None,
) -> dict:
    billing_stats = billing_stats or {}
    payload = {
        "id": row.id,
        "name": row.name,
        "provider_platform": row.provider_platform,
        "channel_type": row.channel_type,
        "base_url": row.base_url,
        "auth_style": row.auth_style,
        "status": row.status,
        "priority": int(row.priority or 0),
        "weight": int(row.weight or 1),
        "allowed_fails": int(row.allowed_fails or 3),
        "cooldown_seconds": float(row.cooldown_seconds or 0),
        "capabilities": _list_from_csv(row.capabilities),
        "provider_account_fingerprint": row.provider_account_fingerprint,
        "api_key_configured": bool(getattr(row, "encrypted_api_key", None)),
        "api_key_fingerprint": _provider_channel_key_fingerprint(row),
        "cost_tier": row.cost_tier,
        "notes": row.notes,
        "route_count": route_count,
        "runtime": _channel_runtime_payload(row.id, runtime_state),
        "billing_stats": {
            "last_1h_cents": int(billing_stats.get("last_1h_cents", 0) or 0),
            "last_4h_cents": int(billing_stats.get("last_4h_cents", 0) or 0),
            "today_cents": int(billing_stats.get("today_cents", 0) or 0),
            "total_cents": int(billing_stats.get("total_cents", 0) or 0),
        },
        "updated_by": row.updated_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    if monitor_selection is not None:
        payload["monitor_selection"] = monitor_selection
    return payload


def _provider_channel_route_choices(
    channel: ProviderChannel,
    routes: list[ModelChannelRoute],
) -> list[dict[str, str]]:
    candidates: list[tuple[tuple[int, int, str], dict[str, str]]] = []
    for route in routes:
        if str(getattr(route, "channel_id", "") or "") != str(channel.id):
            continue
        if str(getattr(route, "status", "") or "").strip().lower() != "active":
            continue
        specs = desired_route_monitor_specs([channel], [route])
        if not specs:
            continue
        spec = specs[0]
        priority = getattr(route, "priority_override", None)
        weight = getattr(route, "weight_override", None)
        sort_key = (
            int(priority if priority is not None else getattr(channel, "priority", 0) or 0),
            -max(1, int(weight if weight is not None else getattr(channel, "weight", 1) or 1)),
            str(getattr(route, "id", "") or ""),
        )
        candidates.append((sort_key, {"model": spec.models[0], "endpoint": spec.endpoint}))

    choices: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for _sort_key, choice in sorted(candidates, key=lambda item: item[0]):
        target = (choice["endpoint"], choice["model"])
        if target not in seen:
            seen.add(target)
            choices.append(choice)
    return choices


def _provider_channel_monitor_selection_payload(
    channel: ProviderChannel,
    routes: list[ModelChannelRoute],
    monitors: list[ProviderChannelMonitor],
) -> dict[str, Any]:
    choices = _provider_channel_route_choices(channel, routes)
    recommended = choices[0] if choices else None
    channel_monitors = sorted(
        (monitor for monitor in monitors if str(getattr(monitor, "channel_id", "") or "") == str(channel.id)),
        key=lambda monitor: str(getattr(monitor, "id", "") or ""),
    )
    valid_targets = {(item["endpoint"], item["model"]) for item in choices}
    manual = [monitor for monitor in channel_monitors if getattr(monitor, "created_by", "") != AUTO_MONITOR_CREATED_BY]
    active_manual = [
        monitor for monitor in manual if str(getattr(monitor, "status", "") or "").lower() == "active"
    ]
    active_auto = [
        monitor
        for monitor in channel_monitors
        if getattr(monitor, "created_by", "") == AUTO_MONITOR_CREATED_BY
        and str(getattr(monitor, "status", "") or "").lower() == "active"
    ]
    valid_active_manual = [
        monitor
        for monitor in active_manual
        if (
            str(getattr(monitor, "endpoint", "") or "").strip(),
            str(getattr(monitor, "primary_model", "") or "").strip(),
        )
        in valid_targets
    ]
    valid_active_auto = [
        monitor
        for monitor in active_auto
        if (
            str(getattr(monitor, "endpoint", "") or "").strip(),
            str(getattr(monitor, "primary_model", "") or "").strip(),
        )
        in valid_targets
    ]
    selected = (
        valid_active_manual or valid_active_auto or active_manual or manual or active_auto or channel_monitors or [None]
    )[0]
    mode = "manual" if selected is not None and getattr(selected, "created_by", "") != AUTO_MONITOR_CREATED_BY else "auto"
    selected_model = str(getattr(selected, "primary_model", "") or "").strip() or None
    selected_endpoint = str(getattr(selected, "endpoint", "") or "").strip() or None
    is_valid = bool(
        selected is not None
        and str(getattr(selected, "status", "") or "").lower() == "active"
        and (selected_endpoint, selected_model) in valid_targets
    )
    state = "valid" if is_valid else ("unconfigured" if not choices and selected is None else "invalid")
    return {
        "mode": mode,
        "selected_model": selected_model,
        "selected_endpoint": selected_endpoint,
        "state": state,
        "is_valid": is_valid,
        "recommended": recommended,
        "choices": choices,
    }


def _provider_channel_monitor_payload(
    monitor: ProviderChannelMonitor,
    channel: Optional[ProviderChannel] = None,
    *,
    availability: dict[str, Any] | None = None,
    timeline: list[dict] | None = None,
) -> dict:
    availability = availability or {}
    return {
        "id": monitor.id,
        "channel_id": monitor.channel_id,
        "channel_name": getattr(channel, "name", "") if channel is not None else "",
        "provider_platform": getattr(channel, "provider_platform", "") if channel is not None else "",
        "channel_type": getattr(channel, "channel_type", "") if channel is not None else "",
        "base_url": getattr(channel, "base_url", "") if channel is not None else "",
        "name": monitor.name,
        "endpoint": monitor.endpoint,
        "primary_model": monitor.primary_model,
        "extra_models": parse_monitor_models(monitor.extra_models),
        "models": monitor_model_list(monitor),
        "status": monitor.status,
        "interval_seconds": int(monitor.interval_seconds or 0),
        "timeout_seconds": int(monitor.timeout_seconds or 0),
        "last_checked_at": monitor.last_checked_at,
        "last_status": monitor.last_status,
        "last_latency_ms": int(monitor.last_latency_ms or 0),
        "last_ping_latency_ms": int(monitor.last_ping_latency_ms or 0),
        "last_message": monitor.last_message,
        "availability_rate": float(availability.get("availability_rate", 0.0) or 0.0),
        "avg_latency_ms": int(availability.get("avg_latency_ms", 0) or 0),
        "avg_ping_latency_ms": int(availability.get("avg_ping_latency_ms", 0) or 0),
        "total_checks": int(availability.get("total_checks", 0) or 0),
        "operational_count": int(availability.get("operational_count", 0) or 0),
        "degraded_count": int(availability.get("degraded_count", 0) or 0),
        "failed_count": int(availability.get("failed_count", 0) or 0),
        "error_count": int(availability.get("error_count", 0) or 0),
        "timeline": timeline or [],
        "created_at": monitor.created_at,
        "updated_at": monitor.updated_at,
    }


def _provider_channel_monitor_action_payload(monitor: ProviderChannelMonitor) -> dict:
    return {
        "id": monitor.id,
        "channel_id": monitor.channel_id,
        "name": monitor.name,
        "endpoint": monitor.endpoint,
        "primary_model": monitor.primary_model,
        "extra_models": parse_monitor_models(monitor.extra_models),
        "status": monitor.status,
        "interval_seconds": int(monitor.interval_seconds or 0),
        "timeout_seconds": int(monitor.timeout_seconds or 0),
    }


async def _lock_provider_channels_for_monitor_change(
    db: AsyncSession,
    channel_ids: set[str],
) -> dict[str, ProviderChannel]:
    ids = sorted({str(channel_id or "") for channel_id in channel_ids if str(channel_id or "")})
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(ProviderChannel)
            .where(ProviderChannel.id.in_(ids))
            .order_by(ProviderChannel.id.asc())
            .with_for_update()
        )
    ).scalars().all()
    return {str(channel.id): channel for channel in rows}


def _model_channel_route_payload(row: ModelChannelRoute, channel: Optional[ProviderChannel] = None) -> dict:
    return {
        "id": row.id,
        "public_model_id": row.public_model_id,
        "endpoint": row.endpoint,
        "channel_id": row.channel_id,
        "channel_name": getattr(channel, "name", "") if channel else "",
        "channel_status": getattr(channel, "status", "") if channel else "",
        "provider_platform": getattr(channel, "provider_platform", "") if channel else "",
        "channel_priority": int(getattr(channel, "priority", 0) or 0) if channel else None,
        "channel_weight": int(getattr(channel, "weight", 1) or 1) if channel else None,
        "upstream_model": row.upstream_model,
        "priority_override": row.priority_override,
        "weight_override": row.weight_override,
        "transform_profile": row.transform_profile,
        "status": row.status,
        "notes": row.notes,
        "updated_by": row.updated_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _validate_model_channel_route(public_model_id: str, endpoint: str = "") -> None:
    model_registry.ensure_initialized()
    model = model_registry.get_public_model(public_model_id)
    if not model:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="public model not found")
    endpoint = (endpoint or "").strip()
    if endpoint and endpoint not in (model.capabilities or ()):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="endpoint is not supported by public model")


def _provider_channel_auth_headers(channel: ProviderChannel) -> dict[str, str]:
    raw_key = _recover_raw_key(getattr(channel, "encrypted_api_key", None))
    if not raw_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="provider channel api key is not configured")
    headers = {"accept": "application/json"}
    auth_style = (getattr(channel, "auth_style", "") or "bearer").strip().lower()
    channel_type = (getattr(channel, "channel_type", "") or "").strip().lower()
    if auth_style == "azure":
        headers["api-key"] = raw_key
    elif auth_style in {"x-api-key", "anthropic_x_api_key", "anthropic"}:
        headers["x-api-key"] = raw_key
    else:
        headers["authorization"] = f"Bearer {raw_key}"
    if channel_type == "anthropic_compatible":
        headers["anthropic-version"] = "2023-06-01"
    return headers


def _provider_channel_models_url_candidates(base_url: str, *, prefer_v1: bool = False) -> list[tuple[str, str]]:
    cleaned = str(base_url or "").strip().rstrip("/")
    if not cleaned:
        return []
    candidates = [(f"{cleaned}/models", cleaned)]
    parsed = urlsplit(cleaned)
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1"):
        normalized_path = f"{path}/v1" if path else "/v1"
        normalized_base = urlunsplit((parsed.scheme, parsed.netloc, normalized_path, parsed.query, parsed.fragment)).rstrip("/")
        candidates.append((f"{normalized_base}/models", normalized_base))
    result = []
    seen = set()
    ordered_candidates = [candidates[-1]] if prefer_v1 and len(candidates) > 1 else candidates
    for url, recommended_base_url in ordered_candidates:
        if url in seen:
            continue
        seen.add(url)
        result.append((url, recommended_base_url))
    return result


def _upstream_model_items(payload: Any) -> list[dict]:
    raw_items = []
    if isinstance(payload, dict):
        for key in ("data", "models", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                raw_items = value
                break
    elif isinstance(payload, list):
        raw_items = payload

    models = []
    seen = set()
    for item in raw_items:
        if isinstance(item, dict):
            model_id = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
            owned_by = str(item.get("owned_by") or item.get("owner") or "").strip()
            created = item.get("created")
            object_type = str(item.get("object") or "model").strip()
        else:
            model_id = str(item or "").strip()
            owned_by = ""
            created = None
            object_type = "model"
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        models.append({
            "id": model_id,
            "object": object_type,
            "owned_by": owned_by,
            "created": created,
        })
    return models[:500]


def _admin_public_model_options() -> list[dict]:
    model_registry.ensure_initialized()
    return [
        {
            "id": model.public_id,
            "owned_by": model.owned_by,
            "provider_name": model.provider_name,
            "provider_model": model.provider_model,
            "upstream_model": model.upstream_model,
            "delivery_lane": model.delivery_lane,
            "capabilities": list(model.capabilities or ()),
        }
        for model in model_registry.list_public_models()
    ]


def _attach_public_model_suggestions(models: list[dict]) -> tuple[list[dict], list[dict]]:
    public_models = _admin_public_model_options()
    public_ids = {item["id"] for item in public_models}
    provider_to_public = {
        str(item.get("provider_model") or "").strip(): item["id"]
        for item in public_models
        if str(item.get("provider_model") or "").strip()
    }
    upstream_to_public = {
        str(item.get("upstream_model") or "").strip(): item["id"]
        for item in public_models
        if str(item.get("upstream_model") or "").strip()
    }
    enriched = []
    for model in models:
        model_id = str(model.get("id") or "").strip()
        suggested = ""
        if model_id in public_ids:
            suggested = model_id
        elif model_id in upstream_to_public:
            suggested = upstream_to_public[model_id]
        elif model_id in provider_to_public:
            suggested = provider_to_public[model_id]
        enriched.append({**model, "suggested_public_model_id": suggested})
    return enriched, public_models


def _provider_channel_messages_url(base_url: str) -> tuple[str, str]:
    cleaned = str(base_url or "").strip().rstrip("/")
    if not cleaned:
        return "", ""
    parsed = urlsplit(cleaned)
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1"):
        path = f"{path}/v1" if path else "/v1"
    normalized_base = urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)).rstrip("/")
    return f"{normalized_base}/messages", normalized_base


def _anthropic_provider_probe_model_id(public_models: list[dict]) -> str:
    for item in public_models:
        if item.get("id") == "claude-fable-5":
            return str(item.get("upstream_model") or item.get("provider_model") or item["id"])
    for item in public_models:
        owned_by = str(item.get("owned_by") or "").strip().lower()
        provider_name = str(item.get("provider_name") or "").strip().lower()
        delivery_lane = str(item.get("delivery_lane") or "").strip().lower()
        capabilities = set(item.get("capabilities") or [])
        if delivery_lane == "route_only" and "chat/completions" in capabilities and (owned_by == "anthropic" or provider_name == "anthropic"):
            return str(item.get("upstream_model") or item.get("provider_model") or item.get("id") or "").strip()
    return "claude-fable-5"


async def _anthropic_provider_messages_probe(client: httpx.AsyncClient, channel: ProviderChannel, headers: dict[str, str], attempts: list[dict]) -> Optional[dict]:
    url, recommended_base_url = _provider_channel_messages_url(getattr(channel, "base_url", ""))
    if not url:
        return None
    _empty_models, public_models = _attach_public_model_suggestions([])
    probe_model = _anthropic_provider_probe_model_id(public_models)
    started = time.perf_counter()
    try:
        response = await client.post(
            url,
            headers=headers,
            json={
                "model": probe_model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            },
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        attempt = {
            "url": url,
            "status_code": response.status_code,
            "latency_ms": latency_ms,
            "discovery_mode": "messages_probe",
        }
        attempts.append(attempt)
        if 200 <= int(response.status_code) < 300:
            model_id = probe_model
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if isinstance(payload, dict):
                model_id = str(payload.get("model") or probe_model).strip() or probe_model
            models, public_models = _attach_public_model_suggestions([
                {
                    "id": model_id,
                    "object": "model",
                    "owned_by": "anthropic",
                    "created": None,
                }
            ])
            return {
                "ok": True,
                "channel_id": channel.id,
                "channel_name": channel.name,
                "models_url": url,
                "recommended_base_url": recommended_base_url,
                "status_code": response.status_code,
                "latency_ms": latency_ms,
                "model_count": len(models),
                "models": models,
                "public_models": public_models,
                "attempts": attempts,
                "discovery_mode": "messages_probe",
            }
        attempt["error"] = f"upstream returned HTTP {response.status_code}"
    except httpx.TimeoutException:
        latency_ms = int((time.perf_counter() - started) * 1000)
        attempts.append({"url": url, "status_code": 0, "latency_ms": latency_ms, "discovery_mode": "messages_probe", "error": "upstream messages probe timed out"})
    except httpx.RequestError as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        attempts.append({"url": url, "status_code": 0, "latency_ms": latency_ms, "discovery_mode": "messages_probe", "error": str(exc)[:256] or "upstream messages probe failed"})
    return None


async def _provider_channel_models_payload(channel: ProviderChannel) -> dict:
    headers = _provider_channel_auth_headers(channel)
    attempts = []
    last_error = ""
    timeout = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        is_anthropic_channel = str(getattr(channel, "channel_type", "") or "").strip().lower() == "anthropic_compatible"
        for url, recommended_base_url in _provider_channel_models_url_candidates(getattr(channel, "base_url", ""), prefer_v1=is_anthropic_channel):
            started = time.perf_counter()
            try:
                response = await client.get(url, headers=headers)
                latency_ms = int((time.perf_counter() - started) * 1000)
                attempt = {
                    "url": url,
                    "status_code": response.status_code,
                    "latency_ms": latency_ms,
                }
                attempts.append(attempt)
                if 200 <= int(response.status_code) < 300:
                    try:
                        raw_payload = response.json()
                    except ValueError:
                        last_error = "upstream returned non-json models payload"
                        attempt["error"] = last_error
                        continue
                    models, public_models = _attach_public_model_suggestions(_upstream_model_items(raw_payload))
                    return {
                        "ok": True,
                        "channel_id": channel.id,
                        "channel_name": channel.name,
                        "models_url": url,
                        "recommended_base_url": recommended_base_url,
                        "status_code": response.status_code,
                        "latency_ms": latency_ms,
                        "model_count": len(models),
                        "models": models,
                        "public_models": public_models,
                        "attempts": attempts,
                    }
                last_error = f"upstream returned HTTP {response.status_code}"
                attempt["error"] = last_error
            except httpx.TimeoutException:
                latency_ms = int((time.perf_counter() - started) * 1000)
                last_error = "upstream models request timed out"
                attempts.append({"url": url, "status_code": 0, "latency_ms": latency_ms, "error": last_error})
            except httpx.RequestError as exc:
                latency_ms = int((time.perf_counter() - started) * 1000)
                last_error = str(exc)[:256] or "upstream models request failed"
                attempts.append({"url": url, "status_code": 0, "latency_ms": latency_ms, "error": last_error})
        if is_anthropic_channel:
            probe_payload = await _anthropic_provider_messages_probe(client, channel, headers, attempts)
            if probe_payload is not None:
                return probe_payload
            if attempts:
                last_error = str((attempts[-1] or {}).get("error") or last_error)

    models, public_models = _attach_public_model_suggestions([])
    return {
        "ok": False,
        "channel_id": channel.id,
        "channel_name": channel.name,
        "models_url": "",
        "recommended_base_url": str(getattr(channel, "base_url", "") or "").strip().rstrip("/"),
        "status_code": int((attempts[-1] or {}).get("status_code") or 0) if attempts else 0,
        "latency_ms": int((attempts[-1] or {}).get("latency_ms") or 0) if attempts else 0,
        "model_count": 0,
        "models": models,
        "public_models": public_models,
        "attempts": attempts,
        "error": last_error or "no upstream models endpoint candidate",
    }


def _add_system_channel_model(groups: dict, key: str, payload: dict, public_model) -> None:
    entry = groups.setdefault(key, {**payload, "model_count": 0, "public_models": [], "capabilities": set()})
    entry["model_count"] = int(entry.get("model_count", 0) or 0) + 1
    entry["public_models"].append(public_model.public_id)
    entry["capabilities"].update(public_model.capabilities or ())


def _catalog_env_default(value):
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not (text.startswith("${") and ":-" in text):
        return value
    fallback = text.split(":-", 1)[1]
    while fallback.endswith("}"):
        fallback = fallback[:-1]
    return fallback


def _admin_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(_catalog_env_default(value))
        except (TypeError, ValueError):
            return default


def _admin_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return float(_catalog_env_default(value))
        except (TypeError, ValueError):
            return default


def _system_default_channel_payloads() -> list[dict]:
    model_registry.ensure_initialized()
    groups: dict[str, dict] = {}
    for public_model in model_registry.list_public_models():
        metadata = public_model.metadata if isinstance(public_model.metadata, dict) else {}
        if public_model.routing_mode == "legacy_auto":
            execution_pool = str(metadata.get("execution_pool") or "cpa_general_pool").strip() or "cpa_general_pool"
            default_slot = str(metadata.get("legacy_default_slot") or "cheap").strip() or "cheap"
            key = f"legacy:{execution_pool}:{default_slot}"
            _add_system_channel_model(
                groups,
                key,
                {
                    "id": f"system:{key}",
                    "name": f"Legacy CPA · {execution_pool}",
                    "provider_platform": "legacy_cpa",
                    "channel_type": "account_pool",
                    "source": "catalog/env",
                    "status": "default",
                    "priority": 0 if default_slot == "premium" else 10,
                    "weight": 1,
                    "allowed_fails": 3,
                    "cooldown_seconds": 30,
                    "notes": f"catalog legacy_auto，默认 slot={default_slot}",
                },
                public_model,
            )
            continue

        if public_model.delivery_lane == gemini_cpa.DELIVERY_LANE:
            raw_channels = metadata.get("cpa_gemini_channels")
            if isinstance(raw_channels, list) and raw_channels:
                channel_items = [item for item in raw_channels if isinstance(item, dict)]
            else:
                channel_items = [{
                    "channel_id": metadata.get("channel_id") or "gemini-cpa-default",
                    "priority": metadata.get("priority"),
                    "weight": metadata.get("weight"),
                    "allowed_fails": metadata.get("allowed_fails"),
                    "cooldown_seconds": metadata.get("cooldown_seconds"),
                    "provider_model": public_model.upstream_model or public_model.provider_model,
                }]
            for item in channel_items:
                channel_id = str(item.get("channel_id") or metadata.get("channel_id") or "gemini-cpa-default").strip()
                provider_model = str(item.get("provider_model") or public_model.upstream_model or public_model.provider_model or "").strip()
                key = f"cpa_gemini:{channel_id}"
                _add_system_channel_model(
                    groups,
                    key,
                    {
                        "id": f"system:{key}",
                        "name": f"Gemini CPA · {channel_id}",
                        "provider_platform": "cpa_gemini",
                        "channel_type": "account_pool",
                        "source": "catalog metadata",
                        "status": "default",
                        "priority": _admin_int(item.get("priority") or metadata.get("priority"), 0),
                        "weight": max(1, _admin_int(item.get("weight") or metadata.get("weight"), 1)),
                        "allowed_fails": max(1, _admin_int(item.get("allowed_fails") or metadata.get("allowed_fails"), 3)),
                        "cooldown_seconds": max(0.0, _admin_float(item.get("cooldown_seconds") or metadata.get("cooldown_seconds"), 30.0)),
                        "notes": f"provider_model={provider_model}" if provider_model else "Gemini CPA catalog route",
                    },
                    public_model,
                )
            continue

        if public_model.delivery_lane in {"upstream_direct", CLAUDE_COMPAT_PROVIDER_KIRO_GO, "gateway", "vertex_direct"}:
            lane = public_model.delivery_lane or "upstream_direct"
            key = f"{lane}:{public_model.provider_name or public_model.owned_by}"
            _add_system_channel_model(
                groups,
                key,
                {
                    "id": f"system:{key}",
                    "name": f"{lane} · {public_model.provider_name or public_model.owned_by}",
                    "provider_platform": lane,
                    "channel_type": "openai_compatible",
                    "source": "catalog/env",
                    "status": "default",
                    "priority": 0,
                    "weight": 1,
                    "allowed_fails": 3,
                    "cooldown_seconds": 30,
                    "notes": "catalog direct route",
                },
                public_model,
            )

    result = []
    for entry in groups.values():
        entry["capabilities"] = sorted(entry.get("capabilities") or [])
        entry["public_models"] = sorted(set(entry.get("public_models") or []))[:12]
        result.append(entry)
    return sorted(result, key=lambda item: (item.get("provider_platform", ""), item.get("name", "")))


def admin_guard(request: Request):
    require_admin(request)


async def _refresh_reliability_after_control_plane_change(
    db: AsyncSession,
    *,
    reconcile_monitors: bool = False,
) -> None:
    if reconcile_monitors:
        try:
            await reconcile_provider_channel_monitors(db)
        except Exception as exc:
            logger.warning("provider monitor reconcile after admin change failed: %s", exc)
            try:
                await db.rollback()
            except Exception as rollback_exc:
                logger.warning("provider monitor reconcile rollback failed: %s", rollback_exc)
    invalidate_reliability_cache()


def _analytics_period(period: str) -> Tuple[str, int, date, datetime]:
    normalized = (period or "today").strip().lower()
    if normalized == "24h":
        normalized = "today"
    days_by_period = {
        "today": 1,
        "7d": 7,
        "30d": 30,
    }
    if normalized not in days_by_period:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unsupported period")
    days = days_by_period[normalized]
    end_day = date.today()
    start_day = end_day - timedelta(days=days - 1)
    since = datetime.utcnow() - timedelta(days=days)
    return normalized, days, start_day, since


def _leaderboard_window(window: str) -> tuple[str, int]:
    normalized = (window or "1h").strip().lower()
    hours_by_window = {
        "1h": 1,
        "4h": 4,
        "24h": 24,
        "day": 24,
        "daily": 24,
    }
    if normalized not in hours_by_window:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unsupported window")
    hours = hours_by_window[normalized]
    return ("24h" if normalized in {"day", "daily"} else normalized), hours


def _user_usage_sort_window(usage_sort: Optional[str]) -> tuple[str, int] | tuple[None, None]:
    normalized = (usage_sort or "").strip().lower()
    if not normalized:
        return None, None
    hours_by_sort = {
        "1d": 24,
        "day": 24,
        "7d": 24 * 7,
    }
    if normalized not in hours_by_sort:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unsupported usage_sort")
    return ("1d" if normalized in {"1d", "day"} else normalized), hours_by_sort[normalized]


def _analytics_period_fields(period: str, days: int, start_day: date, since: datetime, *, end_at: Optional[datetime] = None) -> dict:
    end_at = end_at or datetime.utcnow()
    if period == "today":
        return {
            "period_label": "近24小时",
            "window_hours": 24,
            "window_start": since,
            "window_end": end_at,
            "start_day": str(since.date()),
            "end_day": str(end_at.date()),
        }
    return {
        "period_label": f"近{days}天",
        "window_hours": days * 24,
        "window_start": _period_start_datetime(start_day),
        "window_end": end_at,
        "start_day": str(start_day),
        "end_day": str(end_at.date()),
    }


def _row_value(row, key: str, default=0):
    if row is None:
        return default
    mapping = getattr(row, "_mapping", None)
    if mapping is not None and key in mapping:
        return mapping[key]
    if hasattr(row, key):
        return getattr(row, key)
    if isinstance(row, dict):
        return row.get(key, default)
    return default


def _display_name(username: Optional[str], email: Optional[str], external_id: Optional[str], user_id: str) -> str:
    return username or email or external_id or user_id


async def _positive_balance_users_count(db: AsyncSession) -> int:
    """Count users whose currently available billing balance is positive without loading user rows."""
    cache_key = "positive_balance_users"
    now_ts = time.time()
    cached = _analytics_balance_cache.get(cache_key)
    if cached and now_ts - cached[0] < ANALYTICS_BALANCE_CACHE_TTL_SECONDS:
        return cached[1]

    current = utcnow()
    active_subscriptions = (
        select(
            UserSubscription.user_id.label("user_id"),
            (UserSubscription.quota_cents - UserSubscription.used_cents).label("remaining_cents"),
        )
        .where(
            UserSubscription.status == "active",
            UserSubscription.paid_until.is_not(None),
            UserSubscription.paid_until > current,
        )
        .subquery()
    )
    traffic_packs = (
        select(
            TrafficPackBalance.user_id.label("user_id"),
            func.coalesce(func.sum(TrafficPackBalance.remaining_cents), 0).label("remaining_cents"),
        )
        .join(active_subscriptions, active_subscriptions.c.user_id == TrafficPackBalance.user_id)
        .where(
            TrafficPackBalance.status == "active",
            TrafficPackBalance.remaining_cents > 0,
            TrafficPackBalance.expires_at > current,
        )
        .group_by(TrafficPackBalance.user_id)
        .subquery()
    )
    active_subscription_balances = (
        select(
            active_subscriptions.c.user_id.label("user_id"),
            (
                case(
                    (active_subscriptions.c.remaining_cents > 0, active_subscriptions.c.remaining_cents),
                    else_=0,
                )
                + func.coalesce(traffic_packs.c.remaining_cents, 0)
            ).label("balance_cents"),
        )
        .outerjoin(traffic_packs, traffic_packs.c.user_id == active_subscriptions.c.user_id)
        .subquery()
    )
    positive_balance_users = await db.scalar(
        select(func.count()).select_from(User)
        .outerjoin(active_subscription_balances, active_subscription_balances.c.user_id == User.id)
        .where(
            (
                func.coalesce(User.balance, 0)
                + func.coalesce(active_subscription_balances.c.balance_cents, 0)
            )
            > 0
        )
    )
    value = int(positive_balance_users or 0)
    _analytics_balance_cache[cache_key] = (now_ts, value)
    return value


def _risk_level(days_remaining: Optional[float], balance_cents: int) -> str:
    if balance_cents <= 0:
        return "critical"
    if days_remaining is None:
        return "unknown"
    if days_remaining <= 3:
        return "critical"
    if days_remaining <= 7:
        return "warning"
    return "watch"


def _period_start_datetime(start_day: date) -> datetime:
    return datetime.combine(start_day, datetime.min.time())


def _safe_rate(numerator: int | float, denominator: int | float) -> float:
    denominator = float(denominator or 0)
    if denominator <= 0:
        return 0.0
    return float(numerator or 0) / denominator


def _date_key(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _request_user_charge_expr():
    return case(
        (func.coalesce(RequestLog.retail_charge_cents, 0) > 0, func.coalesce(RequestLog.retail_charge_cents, 0)),
        else_=func.coalesce(RequestLog.cost_cents, 0),
    )


def _request_upstream_cost_expr():
    return func.coalesce(RequestLog.wholesale_cost_cents, 0)


def _request_channel_type_expr():
    route = func.lower(func.coalesce(RequestLog.route_reason, ""))
    inferred = case(
        (
            route.like("%cpa_gemini%") | route.like("%legacy%"),
            "account_pool",
        ),
        (
            route.like("%upstream_direct%") | route.like("%vertex_direct%") | route.like("%kiro_go%"),
            "official_provider",
        ),
        else_="unknown",
    )
    return func.coalesce(func.nullif(RequestLog.channel_type, ""), inferred)


def _provider_channel_billing_stats_by_channel(rows, *, now: Optional[datetime] = None) -> dict[str, dict[str, int]]:
    results: dict[str, dict[str, int]] = {}

    for row in rows:
        channel_id = str(_row_value(row, "channel_id", "") or row[0] or "")
        if not channel_id:
            continue
        results[channel_id] = {
            "last_1h_cents": int(_row_value(row, "last_1h_cents", 0) or 0),
            "last_4h_cents": int(_row_value(row, "last_4h_cents", 0) or 0),
            "today_cents": int(_row_value(row, "today_cents", 0) or 0),
            "total_cents": int(_row_value(row, "total_cents", 0) or 0),
        }
    return results


def _billing_package_entry_types():
    return ("usage_subscription_debit", "usage_traffic_pack_debit")


def _is_test_identity(*values: Optional[str]) -> bool:
    text = " ".join(str(value or "").lower() for value in values)
    markers = ("test", "smoke", "demo", "dummy", "internal", "codex_", "probe")
    return any(marker in text for marker in markers)


def _analytics_meta(*, generated_at: datetime, cache_hit: bool) -> dict:
    return {
        "generated_at": generated_at,
        "cache_hit": cache_hit,
        "cache_ttl_seconds": ANALYTICS_DASHBOARD_CACHE_TTL_SECONDS,
        "freshness": "cached" if cache_hit else "fresh",
        "freshness_note": (
            f"命中 {ANALYTICS_DASHBOARD_CACHE_TTL_SECONDS}s 内存缓存，dashboard 快速打开。"
            if cache_hit
            else f"本次从数据库实时聚合；结果会缓存 {ANALYTICS_DASHBOARD_CACHE_TTL_SECONDS}s。"
        ),
    }


def _source_quality(*, upstream_cost_cents: int, user_charge_cents: int, channel_known_rate: float = 0.0) -> dict:
    upstream_known = upstream_cost_cents > 0
    return {
        "upstream_cost_available": upstream_known,
        "upstream_cost_coverage": "partial" if upstream_known and upstream_cost_cents < user_charge_cents else ("available" if upstream_known else "missing"),
        "channel_type_available": channel_known_rate > 0,
        "channel_type_confidence": "request_log_field_or_route_reason_fallback",
        "channel_known_rate": channel_known_rate,
        "missing_fields": [
            field
            for field, missing in {
                "channel_type": channel_known_rate < 1,
                "channel_id": channel_known_rate < 1,
                "provider_account_fingerprint": channel_known_rate < 1,
                "upstream_cost": not upstream_known,
            }.items()
            if missing
        ],
    }


def _build_action_items(
    *,
    period: str,
    days: int,
    low_balance: dict,
    errors: dict,
    channel: dict,
    revenue: dict,
    usage: dict,
    limit: int,
) -> dict:
    items = []

    for user in (low_balance.get("data") or []):
        if _is_test_identity(
            user.get("display_name"),
            user.get("username"),
            user.get("email"),
            user.get("external_id"),
        ):
            continue
        if len([item for item in items if item.get("type") == "low_balance_top_user"]) >= 1:
            break
        days_remaining = user.get("estimated_days_remaining")
        if days_remaining is not None and float(days_remaining) <= 2:
            items.append({
                "severity": "high",
                "type": "low_balance_top_user",
                "owner": "bd",
                "title": f"{user.get('display_name') or user.get('user_id')} 余额预计不足 2 天",
                "evidence": {
                    "user_id": user.get("user_id"),
                    "balance_cents": user.get("balance_cents"),
                    "avg_daily_cost_cents": user.get("avg_daily_cost_cents"),
                    "estimated_days_remaining": days_remaining,
                },
                "suggested_action": "尽快联系用户充值或确认是否需要企业方案。",
            })

    if int(revenue.get("paid_cents") or 0) <= 500 and int(revenue.get("user_charge_cents") or 0) >= 5000:
        items.append({
            "severity": "high",
            "type": "growth_conversion_gap",
            "owner": "product",
            "title": "近24小时消耗主要来自存量用户，新接入/首充不足",
            "evidence": {
                "paid_cents": revenue.get("paid_cents"),
                "user_charge_cents": revenue.get("user_charge_cents"),
                "period": period,
            },
            "suggested_action": "检查注册到首充漏斗和拉新渠道；确认是否需要运营触达高消耗未续费用户。",
        })

    if float(errors.get("error_rate") or 0) > 0.05 or int(errors.get("failed_requests") or 0) >= 10:
        top_model = (errors.get("by_model") or [{}])[0]
        items.append({
            "severity": "high",
            "type": "model_error_rate",
            "owner": "tech",
            "title": f"失败率 {float(errors.get('error_rate') or 0) * 100:.1f}%，需排查模型/通道",
            "evidence": {
                "failed_requests": errors.get("failed_requests"),
                "total_requests": errors.get("total_requests"),
                "top_model": top_model,
            },
            "suggested_action": "检查最近失败请求、上游状态和必要的模型降权/切换。",
        })

    for item in usage.get("data") or []:
        if float(item.get("failure_rate") or 0) > 0.05 and int(item.get("requests") or 0) >= 5:
            items.append({
                "severity": "medium",
                "type": "usage_model_failure",
                "owner": "tech",
                "title": f"{item.get('model')} 失败率偏高",
                "evidence": {
                    "model": item.get("model"),
                    "billable_sku": item.get("billable_sku"),
                    "requests": item.get("requests"),
                    "failure_rate": item.get("failure_rate"),
                },
                "suggested_action": "检查该 SKU 的上游错误和路由策略。",
            })
            break

    for item in usage.get("data") or []:
        avg_latency_ms = int(item.get("avg_latency_ms") or 0)
        requests = int(item.get("requests") or 0)
        if avg_latency_ms >= 12000 and requests >= 5:
            items.append({
                "severity": "high" if avg_latency_ms >= 30000 else "medium",
                "type": "high_latency_model",
                "owner": "tech",
                "title": f"{item.get('model')} 平均延迟 {avg_latency_ms // 1000}s，影响可用性",
                "evidence": {
                    "model": item.get("model"),
                    "billable_sku": item.get("billable_sku"),
                    "requests": requests,
                    "avg_latency_ms": avg_latency_ms,
                    "user_charge_cents": item.get("user_charge_cents"),
                },
                "suggested_action": "检查该模型上游耗时、图片任务同步路径和是否需要路由降级/异步化。",
            })
            break

    if not (revenue.get("source_quality") or {}).get("upstream_cost_available"):
        items.append({
            "severity": "high",
            "type": "missing_upstream_cost",
            "owner": "tech",
            "title": "缺上游真实成本，无法判断近24小时是否赚钱",
            "evidence": {
                "user_charge_cents": revenue.get("user_charge_cents"),
                "upstream_cost_cents": revenue.get("upstream_cost_cents"),
            },
            "suggested_action": "补齐 RequestLog.wholesale_cost_cents 写入或接入 provider 成本回填。",
        })

    if (channel.get("source_quality") or {}).get("channel_known_rate", 0) < 1:
        items.append({
            "severity": "high",
            "type": "missing_channel_type",
            "owner": "tech",
            "title": "缺稳定 channel_type，无法准确拆号池/官方通道",
            "evidence": channel.get("source_quality"),
            "suggested_action": "优先接入后台 provider channel route；新请求会写入 channel_type、channel_id 和 provider_account_fingerprint。",
        })

    if int(revenue.get("paid_cents") or 0) == 0 and int(revenue.get("user_charge_cents") or 0) > 0:
        items.append({
            "severity": "medium",
            "type": "weak_cash_in",
            "owner": "ops",
            "title": "近24小时有消耗但没有实付入账",
            "evidence": {
                "paid_cents": revenue.get("paid_cents"),
                "user_charge_cents": revenue.get("user_charge_cents"),
                "package_consumption_cents": revenue.get("package_consumption_cents"),
            },
            "suggested_action": "检查高消耗用户余额和套餐消耗，推动充值或套餐续费。",
        })

    if not items:
        items.append({
            "severity": "low",
            "type": "daily_watch",
            "owner": "ops",
            "title": "暂无高优先级异常，保持观察 Top 消耗用户",
            "evidence": {
                "paid_cents": revenue.get("paid_cents"),
                "user_charge_cents": revenue.get("user_charge_cents"),
                "error_rate": errors.get("error_rate"),
            },
            "suggested_action": "运营查看 Top 用户是否需要续费提醒；技术继续观察错误趋势。",
        })
    return {"period": period, "days": days, "limit": limit, "items": items[:limit]}


def _claude_compat_settings_payload():
    current_provider = model_registry.current_claude_compat_provider()
    base_url = str(getattr(_settings, "claude_compat_base_url", "") or "").strip()
    api_key = str(getattr(_settings, "claude_compat_api_key", "") or "").strip()
    return {
        "provider": current_provider,
        "options": [
            {
                "id": CLAUDE_COMPAT_PROVIDER_UPSTREAM_DIRECT,
                "label": "兼容直连上游",
                "description": "继续使用当前 OpenAI/Azure 兼容上游，保持旧 Claude 兼容路线。",
            },
            {
                "id": CLAUDE_COMPAT_PROVIDER_KIRO_GO,
                "label": "Kiro-Go",
                "description": "Claude 别名改走 Kiro-Go；/messages 原生直连，/responses 由 CoinCoin 本地兼容桥接。",
            },
        ],
        "configured": {
            "kiro_go_base_url": bool(base_url),
            "kiro_go_api_key": bool(api_key),
        },
        "base_url": base_url or None,
    }


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


@router.get("/model-aliases", dependencies=[Depends(admin_guard)])
async def list_model_aliases(db: AsyncSession = Depends(get_db)):
    await refresh_model_alias_registry_from_db(db)
    aliases = model_registry.list_admin_aliases()
    return {
        "aliases": aliases,
        "override_count": sum(1 for item in aliases if item.get("override_active")),
    }


@router.get("/model-aliases/{alias_id}", dependencies=[Depends(admin_guard)])
async def get_model_alias(alias_id: str, db: AsyncSession = Depends(get_db)):
    await refresh_model_alias_registry_from_db(db)
    payload = _alias_payload(alias_id)
    if not payload:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="alias not found")
    return payload


@router.patch("/model-aliases/{alias_id}", dependencies=[Depends(admin_guard)])
async def update_model_alias(alias_id: str, payload: AdminModelAliasUpdate, db: AsyncSession = Depends(get_db)):
    await refresh_model_alias_registry_from_db(db)
    alias = model_registry.get_admin_alias(alias_id)
    if not alias:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="alias not found")

    override = {}
    if payload.target_alias:
        target = _matching_target(alias_id, payload.target_alias.strip())
        if not target:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="target alias is not compatible")
        override["provider_model"] = target.get("provider_model") or ""
        override["upstream_model"] = target.get("upstream_model") or target.get("provider_model") or ""
    else:
        provider_model = payload.provider_model.strip() if payload.provider_model is not None else ""
        upstream_model = payload.upstream_model.strip() if payload.upstream_model is not None else ""
        if provider_model or upstream_model:
            target = _matching_target_by_models(alias_id, provider_model, upstream_model or provider_model)
            if not target:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="target model is not compatible")
            override["provider_model"] = target.get("provider_model") or ""
            override["upstream_model"] = target.get("upstream_model") or target.get("provider_model") or ""

    if payload.enabled is not None:
        override["enabled"] = payload.enabled

    if not override:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no override fields provided")

    existing = (await db.execute(select(ModelAliasOverride).where(ModelAliasOverride.alias_id == alias_id))).scalar_one_or_none()
    if existing is None:
        existing = ModelAliasOverride(alias_id=alias_id)
        db.add(existing)
    if "provider_model" in override:
        existing.provider_model = override["provider_model"]
    if "upstream_model" in override:
        existing.upstream_model = override["upstream_model"]
    if "enabled" in override:
        existing.enabled = 1 if override["enabled"] else 0
    existing.updated_by = "admin"
    await db.commit()
    apply_runtime_alias_override(alias_id, override)

    updated = _alias_payload(alias_id)
    if not updated:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="alias update failed")
    return updated


@router.delete("/model-aliases/{alias_id}", dependencies=[Depends(admin_guard)])
async def clear_model_alias_override(alias_id: str, db: AsyncSession = Depends(get_db)):
    existing = (await db.execute(select(ModelAliasOverride).where(ModelAliasOverride.alias_id == alias_id))).scalar_one_or_none()
    if existing is not None:
        await db.delete(existing)
        await db.commit()
    clear_runtime_alias_override(alias_id)

    payload = _alias_payload(alias_id)
    if not payload:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="alias not found")
    return payload


@router.get("/model-pricing", dependencies=[Depends(admin_guard)])
async def list_model_pricing(db: AsyncSession = Depends(get_db)):
    await refresh_model_pricing_registry_from_db(db)
    models = [_pricing_payload(model.public_id) for model in model_registry.list_public_models()]
    models = [model for model in models if model is not None]
    return {
        "models": models,
        "override_count": sum(1 for item in models if item.get("override_active")),
    }


@router.get("/model-pricing/{model_id}", dependencies=[Depends(admin_guard)])
async def get_model_pricing(model_id: str, db: AsyncSession = Depends(get_db)):
    await refresh_model_pricing_registry_from_db(db)
    payload = _pricing_payload(model_id)
    if not payload:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="model not found")
    return payload


@router.patch("/model-pricing/{model_id}", dependencies=[Depends(admin_guard)])
async def update_model_pricing(model_id: str, payload: AdminModelPricingUpdate, db: AsyncSession = Depends(get_db)):
    await refresh_model_pricing_registry_from_db(db)
    if not model_registry.get_public_model(model_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="model not found")
    if not payload.model_fields_set:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no pricing fields provided")

    existing = (
        await db.execute(select(ModelPricingOverride).where(ModelPricingOverride.model_id == model_id))
    ).scalar_one_or_none()
    if existing is None:
        existing = ModelPricingOverride(model_id=model_id)
        db.add(existing)
    if payload.model_multiplier is not None:
        existing.model_multiplier = float(payload.model_multiplier)
    if payload.output_multiplier is not None:
        existing.output_multiplier = float(payload.output_multiplier)
    if payload.cache_read_multiplier is not None:
        existing.cache_read_multiplier = float(payload.cache_read_multiplier)
    if payload.image_multiplier is not None:
        existing.image_multiplier = float(payload.image_multiplier)
    if payload.video_multiplier is not None:
        existing.video_multiplier = float(payload.video_multiplier)
    existing.pricing_mode = "multiplier"
    existing.price_version = int(existing.price_version or 0) + 1
    existing.updated_by = "admin"
    await db.commit()
    await refresh_model_pricing_registry_from_db(db)
    updated = _pricing_payload(model_id)
    if not updated:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="pricing update failed")
    return updated


@router.delete("/model-pricing/{model_id}", dependencies=[Depends(admin_guard)])
async def clear_model_pricing(model_id: str, db: AsyncSession = Depends(get_db)):
    existing = (
        await db.execute(select(ModelPricingOverride).where(ModelPricingOverride.model_id == model_id))
    ).scalar_one_or_none()
    if existing is not None:
        await db.delete(existing)
        await db.commit()
    await refresh_model_pricing_registry_from_db(db)
    payload = _pricing_payload(model_id)
    if not payload:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="model not found")
    return payload


@router.get("/provider-channels", dependencies=[Depends(admin_guard)])
async def list_provider_channels(db: AsyncSession = Depends(get_db)):
    channel_rows = (
        await db.execute(select(ProviderChannel).order_by(ProviderChannel.priority.asc(), ProviderChannel.name.asc()))
    ).scalars().all()
    route_count_rows = (
        await db.execute(
            select(ModelChannelRoute.channel_id, func.count(ModelChannelRoute.id).label("route_count"))
            .group_by(ModelChannelRoute.channel_id)
        )
    ).all()
    route_counts = {
        str(_row_value(row, "channel_id", "") or row[0] or ""): int(_row_value(row, "route_count", 0) or row[1] or 0)
        for row in route_count_rows
    }
    now = datetime.utcnow()
    since_1h = now - timedelta(hours=1)
    since_4h = now - timedelta(hours=4)
    china_day = (now + timedelta(hours=8)).date()
    today_start = datetime.combine(china_day, datetime.min.time()) - timedelta(hours=8)
    request_charge = _request_user_charge_expr()
    billing_rows = (
        await db.execute(
            select(
                RequestLog.channel_id.label("channel_id"),
                func.coalesce(func.sum(case((RequestLog.created_at >= since_1h, request_charge), else_=0)), 0).label("last_1h_cents"),
                func.coalesce(func.sum(case((RequestLog.created_at >= since_4h, request_charge), else_=0)), 0).label("last_4h_cents"),
                func.coalesce(func.sum(case((RequestLog.created_at >= today_start, request_charge), else_=0)), 0).label("today_cents"),
                func.coalesce(func.sum(request_charge), 0).label("total_cents"),
            )
            .where(RequestLog.channel_id != "")
            .group_by(RequestLog.channel_id)
        )
    ).all()
    billing_by_channel = _provider_channel_billing_stats_by_channel(billing_rows)
    runtime_rows = (await db.execute(select(ProviderChannelRuntimeState))).scalars().all()
    runtime_by_channel = {row.channel_id: row for row in runtime_rows}
    route_rows = (await db.execute(select(ModelChannelRoute))).scalars().all()
    monitor_rows = (await db.execute(select(ProviderChannelMonitor))).scalars().all()
    return {
        "channels": [
            _provider_channel_payload(
                row,
                route_count=route_counts.get(row.id, 0),
                runtime_state=runtime_by_channel.get(row.id),
                billing_stats=billing_by_channel.get(row.id),
                monitor_selection=_provider_channel_monitor_selection_payload(row, route_rows, monitor_rows),
            )
            for row in channel_rows
        ],
        "default_channels": _system_default_channel_payloads(),
        "router_version": channel_router.version,
    }


@router.get("/provider-channels/stability", dependencies=[Depends(admin_guard)])
async def provider_channel_stability(period: str = "7d", limit: int = 60, db: AsyncSession = Depends(get_db)):
    days_by_period = {"7d": 7, "15d": 15, "30d": 30}
    normalized = (period or "7d").strip().lower()
    if normalized not in days_by_period:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unsupported period")
    limit = min(120, max(20, int(limit or 60)))
    since = datetime.utcnow() - timedelta(days=days_by_period[normalized])

    channel_rows = (await db.execute(select(ProviderChannel))).scalars().all()
    runtime_rows = (await db.execute(select(ProviderChannelRuntimeState))).scalars().all()
    runtime_by_channel = {row.channel_id: row for row in runtime_rows}

    stats_rows = (
        await db.execute(
            select(
                RequestLog.channel_id.label("channel_id"),
                RequestLog.provider_platform.label("provider_platform"),
                RequestLog.channel_type.label("channel_type"),
                RequestLog.provider_account_fingerprint.label("provider_account_fingerprint"),
                func.count(RequestLog.id).label("requests"),
                func.coalesce(func.sum(case((RequestLog.status_code < 400, 1), else_=0)), 0).label("success_requests"),
                func.coalesce(func.sum(case((RequestLog.status_code >= 400, 1), else_=0)), 0).label("failed_requests"),
                func.coalesce(func.sum(case((RequestLog.route_attempt > 0, 1), else_=0)), 0).label("fallback_in_requests"),
                func.coalesce(func.avg(RequestLog.duration_ms), 0).label("avg_latency_ms"),
                func.coalesce(func.max(RequestLog.duration_ms), 0).label("max_latency_ms"),
                func.max(RequestLog.created_at).label("last_seen_at"),
            )
            .where(RequestLog.created_at >= since, RequestLog.channel_id != "")
            .group_by(
                RequestLog.channel_id,
                RequestLog.provider_platform,
                RequestLog.channel_type,
                RequestLog.provider_account_fingerprint,
            )
            .order_by(func.count(RequestLog.id).desc())
        )
    ).all()
    fallback_out_rows = (
        await db.execute(
            select(
                RequestLog.fallback_from_channel_id.label("channel_id"),
                func.count(RequestLog.id).label("fallback_out_requests"),
            )
            .where(RequestLog.created_at >= since, RequestLog.fallback_from_channel_id != "")
            .group_by(RequestLog.fallback_from_channel_id)
        )
    ).all()
    recent_rows = (
        await db.execute(
            select(RequestLog)
            .where(RequestLog.created_at >= since, RequestLog.channel_id != "")
            .order_by(RequestLog.created_at.desc())
            .limit(max(500, limit * max(1, len(channel_rows))))
        )
    ).scalars().all()

    stats_by_channel = {str(_row_value(row, "channel_id", "") or ""): row for row in stats_rows}
    fallback_out_by_channel = {
        str(_row_value(row, "channel_id", "") or ""): int(_row_value(row, "fallback_out_requests", 0) or 0)
        for row in fallback_out_rows
    }
    channel_meta: dict[str, dict] = {}
    for row in channel_rows:
        payload = _provider_channel_payload(row, runtime_state=runtime_by_channel.get(row.id))
        channel_meta[row.id] = payload

    recent_by_channel: dict[str, list[dict]] = {}
    for log in recent_rows:
        channel_id = str(getattr(log, "channel_id", "") or "")
        if not channel_id:
            continue
        bucket = recent_by_channel.setdefault(channel_id, [])
        if len(bucket) >= limit:
            continue
        status_label = "failed" if int(getattr(log, "status_code", 0) or 0) >= 400 else "ok"
        if status_label == "ok" and int(getattr(log, "route_attempt", 0) or 0) > 0:
            status_label = "fallback"
        bucket.append({
            "at": getattr(log, "created_at", None),
            "status": status_label,
            "status_code": int(getattr(log, "status_code", 0) or 0),
            "latency_ms": int(getattr(log, "duration_ms", 0) or 0),
            "model": getattr(log, "model", "") or getattr(log, "customer_model_alias", ""),
            "route_reason": getattr(log, "route_reason", "") or "",
            "route_attempt": int(getattr(log, "route_attempt", 0) or 0),
        })

    channel_ids = set(channel_meta.keys()) | set(stats_by_channel.keys()) | set(fallback_out_by_channel.keys())
    items = []
    for channel_id in sorted(channel_ids):
        meta = channel_meta.get(channel_id) or {}
        stat = stats_by_channel.get(channel_id)
        requests = int(_row_value(stat, "requests", 0) or 0)
        success = int(_row_value(stat, "success_requests", 0) or 0)
        failed = int(_row_value(stat, "failed_requests", 0) or 0)
        fallback_in = int(_row_value(stat, "fallback_in_requests", 0) or 0)
        fallback_out = int(fallback_out_by_channel.get(channel_id, 0) or 0)
        availability_rate = _safe_rate(success, requests)
        runtime = meta.get("runtime") or _channel_runtime_payload(channel_id, runtime_by_channel.get(channel_id))
        cooling = int(runtime.get("memory_cooldown_remaining_seconds") or 0)
        if requests <= 0:
            health_status = "idle"
        elif cooling > 0:
            health_status = "cooling"
        elif availability_rate >= 0.98 and fallback_out == 0:
            health_status = "operational"
        elif availability_rate >= 0.90:
            health_status = "degraded"
        else:
            health_status = "failed"
        provider_platform = (
            meta.get("provider_platform")
            or _row_value(stat, "provider_platform", "")
            or ("system" if channel_id.startswith("system:") else "")
        )
        channel_type = meta.get("channel_type") or _row_value(stat, "channel_type", "") or ""
        items.append({
            "channel_id": channel_id,
            "name": meta.get("name") or channel_id,
            "provider_platform": provider_platform,
            "channel_type": channel_type,
            "base_url": meta.get("base_url", ""),
            "status": meta.get("status") or ("default" if channel_id.startswith("system:") else ""),
            "health_status": health_status,
            "availability_rate": availability_rate,
            "requests": requests,
            "success_requests": success,
            "failed_requests": failed,
            "failure_rate": _safe_rate(failed, requests),
            "fallback_in_requests": fallback_in,
            "fallback_out_requests": fallback_out,
            "avg_latency_ms": int(float(_row_value(stat, "avg_latency_ms", 0) or 0)),
            "max_latency_ms": int(float(_row_value(stat, "max_latency_ms", 0) or 0)),
            "last_seen_at": _row_value(stat, "last_seen_at", None),
            "runtime": runtime,
            "recent": list(reversed(recent_by_channel.get(channel_id, []))),
        })
    items.sort(key=lambda item: (0 if item["health_status"] in {"failed", "cooling", "degraded"} else 1, -item["requests"], item["name"]))
    total_requests = sum(item["requests"] for item in items)
    total_failed = sum(item["failed_requests"] for item in items)
    total_fallback_out = sum(item["fallback_out_requests"] for item in items)
    return {
        "period": normalized,
        "window_days": days_by_period[normalized],
        "window_start": since,
        "limit": limit,
        "summary": {
            "channels": len(items),
            "requests": total_requests,
            "failed_requests": total_failed,
            "availability_rate": 1.0 - _safe_rate(total_failed, total_requests) if total_requests else 0.0,
            "fallback_out_requests": total_fallback_out,
        },
        "items": items,
    }


def _monitor_period_days(period: str) -> tuple[str, int]:
    days_by_period = {"7d": 7, "15d": 15, "30d": 30}
    normalized = (period or "7d").strip().lower()
    if normalized not in days_by_period:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unsupported period")
    return normalized, days_by_period[normalized]


async def _monitor_timeline_map(db: AsyncSession, monitors: list[ProviderChannelMonitor], *, limit: int = 60) -> dict[str, list[dict]]:
    if not monitors:
        return {}
    primary_by_id = {monitor.id: monitor.primary_model for monitor in monitors}
    rows = (
        await db.execute(
            select(ProviderChannelMonitorHistory)
            .where(ProviderChannelMonitorHistory.monitor_id.in_(list(primary_by_id.keys())))
            .order_by(ProviderChannelMonitorHistory.checked_at.desc())
            .limit(max(500, len(monitors) * max(20, int(limit or 60))))
        )
    ).scalars().all()
    result: dict[str, list[dict]] = {}
    for row in rows:
        if row.model != primary_by_id.get(row.monitor_id):
            continue
        bucket = result.setdefault(row.monitor_id, [])
        if len(bucket) >= limit:
            continue
        bucket.append({
            "status": row.status,
            "latency_ms": int(row.latency_ms or 0),
            "ping_latency_ms": int(row.ping_latency_ms or 0),
            "status_code": int(row.status_code or 0),
            "message": row.message,
            "checked_at": row.checked_at,
        })
    return {key: list(reversed(value)) for key, value in result.items()}


@router.get("/provider-channel-monitors", dependencies=[Depends(admin_guard)])
async def list_provider_channel_monitors(
    period: str = "7d",
    search: str = "",
    status_filter: str = "",
    provider: str = "",
    channel_id: str = "",
    db: AsyncSession = Depends(get_db),
):
    normalized, days = _monitor_period_days(period)
    search = (search or "").strip().lower()
    status_filter = (status_filter or "").strip().lower()
    provider = (provider or "").strip().lower()
    channel_id = (channel_id or "").strip()
    if status_filter and status_filter not in {"active", "disabled"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unsupported monitor status")
    rows = (
        await db.execute(
            select(ProviderChannelMonitor, ProviderChannel)
            .outerjoin(ProviderChannel, ProviderChannel.id == ProviderChannelMonitor.channel_id)
            .order_by(ProviderChannelMonitor.status.asc(), ProviderChannelMonitor.name.asc(), ProviderChannelMonitor.created_at.desc())
        )
    ).all()
    if status_filter:
        rows = [row for row in rows if str(row[0].status or "").strip().lower() == status_filter]
    if provider:
        rows = [row for row in rows if provider in str((row[1].provider_platform if row[1] else "") or "").lower()]
    if channel_id:
        rows = [row for row in rows if str(row[0].channel_id or "") == channel_id]
    if search:
        def _matches_monitor(row: tuple[ProviderChannelMonitor, Optional[ProviderChannel]]) -> bool:
            monitor, channel = row
            haystack = " ".join([
                monitor.id or "",
                monitor.name or "",
                monitor.channel_id or "",
                monitor.endpoint or "",
                monitor.primary_model or "",
                monitor.extra_models or "",
                channel.name if channel else "",
                channel.provider_platform if channel else "",
                channel.base_url if channel else "",
            ]).lower()
            return search in haystack

        rows = [row for row in rows if _matches_monitor(row)]
    monitors = [row[0] for row in rows]
    availability = await monitor_availability_rows(db, window_days=days)
    timelines = await _monitor_timeline_map(db, monitors, limit=60)
    items = []
    for monitor, channel in rows:
        key = f"{monitor.id}:{monitor.primary_model}"
        items.append(
            _provider_channel_monitor_payload(
                monitor,
                channel,
                availability=availability.get(key),
                timeline=timelines.get(monitor.id, []),
            )
        )
    summary = {
        "total": len(items),
        "active": sum(1 for item in items if item.get("status") == "active"),
        "disabled": sum(1 for item in items if item.get("status") == "disabled"),
        "operational": sum(1 for item in items if item.get("last_status") == "operational"),
        "degraded": sum(1 for item in items if item.get("last_status") == "degraded"),
        "failed": sum(1 for item in items if item.get("last_status") in {"failed", "error"}),
    }
    return {"period": normalized, "window_days": days, "summary": summary, "items": items}


@router.post("/provider-channel-monitors", dependencies=[Depends(admin_guard)])
async def create_provider_channel_monitor(payload: AdminProviderChannelMonitorCreate, db: AsyncSession = Depends(get_db)):
    try:
        channels = await _lock_provider_channels_for_monitor_change(db, {payload.channel_id})
        channel = channels.get(payload.channel_id)
        if channel is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider channel not found")
        monitor = ProviderChannelMonitor(
            id=generate_id("cmon_"),
            channel_id=payload.channel_id,
            name=(payload.name or channel.name or payload.primary_model).strip(),
            endpoint=payload.endpoint,
            primary_model=payload.primary_model.strip(),
            extra_models=serialize_monitor_models(payload.extra_models),
            status=payload.status,
            interval_seconds=int(payload.interval_seconds or _settings.provider_channel_monitor_default_interval),
            timeout_seconds=int(payload.timeout_seconds or _settings.provider_channel_monitor_default_timeout),
            created_by=MANUAL_MONITOR_CREATED_BY if payload.status == "active" else "admin",
        )
        db.add(monitor)
        await db.flush()
        await reconcile_provider_channel_monitors(db, commit=False)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    invalidate_reliability_cache()
    return JSONResponse(content=jsonable_encoder(_provider_channel_monitor_action_payload(monitor)))


@router.patch("/provider-channel-monitors/{monitor_id}", dependencies=[Depends(admin_guard)])
async def update_provider_channel_monitor(
    monitor_id: str,
    payload: AdminProviderChannelMonitorUpdate,
    db: AsyncSession = Depends(get_db),
):
    if not payload.model_fields_set:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no monitor fields provided")
    try:
        monitor = await db.scalar(
            select(ProviderChannelMonitor)
            .where(ProviderChannelMonitor.id == monitor_id)
            .with_for_update()
        )
        if monitor is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel monitor not found")
        fields = payload.model_fields_set
        target_channel_id = (
            payload.channel_id
            if "channel_id" in fields and payload.channel_id is not None
            else monitor.channel_id
        )
        channels = await _lock_provider_channels_for_monitor_change(
            db,
            {str(monitor.channel_id), str(target_channel_id)},
        )
        if str(target_channel_id) not in channels:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider channel not found")
        if "channel_id" in fields and payload.channel_id is not None:
            monitor.channel_id = payload.channel_id
        if "name" in fields and payload.name is not None:
            monitor.name = payload.name.strip()
        if "endpoint" in fields and payload.endpoint is not None:
            monitor.endpoint = payload.endpoint
        if "primary_model" in fields and payload.primary_model is not None:
            monitor.primary_model = payload.primary_model.strip()
        if "extra_models" in fields:
            monitor.extra_models = serialize_monitor_models(payload.extra_models or [])
        if "status" in fields and payload.status is not None:
            monitor.status = payload.status
        if "interval_seconds" in fields and payload.interval_seconds is not None:
            monitor.interval_seconds = int(payload.interval_seconds)
        if "timeout_seconds" in fields and payload.timeout_seconds is not None:
            monitor.timeout_seconds = int(payload.timeout_seconds)
        if str(monitor.status or "").strip().lower() == "active":
            monitor.created_by = MANUAL_MONITOR_CREATED_BY
        await db.flush()
        await reconcile_provider_channel_monitors(db, commit=False)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    invalidate_reliability_cache()
    return JSONResponse(content=jsonable_encoder(_provider_channel_monitor_action_payload(monitor)))


@router.delete("/provider-channel-monitors/{monitor_id}", dependencies=[Depends(admin_guard)])
async def delete_provider_channel_monitor(monitor_id: str, db: AsyncSession = Depends(get_db)):
    monitor = await db.get(ProviderChannelMonitor, monitor_id)
    if monitor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel monitor not found")
    await db.execute(delete(ProviderChannelMonitorHistory).where(ProviderChannelMonitorHistory.monitor_id == monitor_id))
    await db.execute(delete(ProviderChannelMonitorDailyRollup).where(ProviderChannelMonitorDailyRollup.monitor_id == monitor_id))
    await db.execute(delete(ProviderChannelMonitor).where(ProviderChannelMonitor.id == monitor_id))
    await db.commit()
    return {"deleted": True, "id": monitor_id}


@router.post("/provider-channel-monitors/{monitor_id}/run", dependencies=[Depends(admin_guard)])
async def run_provider_channel_monitor_now(monitor_id: str, db: AsyncSession = Depends(get_db)):
    try:
        monitor = await claim_provider_channel_monitor_for_run(db, monitor_id)
    except ProviderChannelMonitorClaimedError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="channel monitor is already running")
    if monitor is None:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel monitor not found")
    try:
        results = await run_provider_channel_monitor_once(db, monitor_id)
    except Exception:
        await db.rollback()
        raise
    invalidate_reliability_cache()
    return JSONResponse(content=jsonable_encoder({
        "monitor_id": monitor_id,
        "results": [
            {
                "model": result.model,
                "status": result.status,
                "latency_ms": result.latency_ms,
                "ping_latency_ms": result.ping_latency_ms,
                "status_code": result.status_code,
                "message": result.message,
                "checked_at": result.checked_at,
            }
            for result in results
        ],
    }))


@router.get("/provider-channel-monitors/{monitor_id}/history", dependencies=[Depends(admin_guard)])
async def list_provider_channel_monitor_history(monitor_id: str, model: str = "", limit: int = 120, db: AsyncSession = Depends(get_db)):
    monitor = await db.get(ProviderChannelMonitor, monitor_id)
    if monitor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel monitor not found")
    limit = min(500, max(1, int(limit or 120)))
    conditions = [ProviderChannelMonitorHistory.monitor_id == monitor_id]
    if model:
        conditions.append(ProviderChannelMonitorHistory.model == model)
    rows = (
        await db.execute(
            select(ProviderChannelMonitorHistory)
            .where(*conditions)
            .order_by(ProviderChannelMonitorHistory.checked_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return {
        "monitor": _provider_channel_monitor_payload(monitor, await db.get(ProviderChannel, monitor.channel_id)),
        "items": [
            {
                "id": row.id,
                "monitor_id": row.monitor_id,
                "channel_id": row.channel_id,
                "model": row.model,
                "status": row.status,
                "latency_ms": int(row.latency_ms or 0),
                "ping_latency_ms": int(row.ping_latency_ms or 0),
                "status_code": int(row.status_code or 0),
                "message": row.message,
                "checked_at": row.checked_at,
            }
            for row in rows
        ],
    }


@router.post("/provider-channels", dependencies=[Depends(admin_guard)])
async def create_provider_channel(payload: AdminProviderChannelCreate, db: AsyncSession = Depends(get_db)):
    channel = ProviderChannel(
        id=generate_id("ch_"),
        name=payload.name.strip(),
        provider_platform=(payload.provider_platform or "").strip(),
        channel_type=(payload.channel_type or "openai_compatible").strip(),
        base_url=payload.base_url.strip().rstrip("/"),
        encrypted_api_key=encrypt_api_key(payload.api_key.strip()),
        auth_style=payload.auth_style,
        status=payload.status,
        priority=int(payload.priority or 0),
        weight=int(payload.weight or 1),
        allowed_fails=int(payload.allowed_fails or 3),
        cooldown_seconds=float(payload.cooldown_seconds or 0),
        capabilities=_csv_from_list(payload.capabilities),
        provider_account_fingerprint=(payload.provider_account_fingerprint or "").strip(),
        cost_tier=(payload.cost_tier or "").strip(),
        notes=payload.notes or "",
        updated_by="admin",
    )
    db.add(channel)
    await db.commit()
    await _refresh_reliability_after_control_plane_change(db)
    await refresh_provider_channel_router_from_db(db)
    return _provider_channel_payload(channel)


@router.get("/provider-channels/{channel_id}", dependencies=[Depends(admin_guard)])
async def get_provider_channel(channel_id: str, db: AsyncSession = Depends(get_db)):
    channel = await db.get(ProviderChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider channel not found")
    route_count = await db.scalar(select(func.count(ModelChannelRoute.id)).where(ModelChannelRoute.channel_id == channel_id)) or 0
    runtime_state = await db.get(ProviderChannelRuntimeState, channel_id)
    routes = (
        await db.execute(select(ModelChannelRoute).where(ModelChannelRoute.channel_id == channel_id))
    ).scalars().all()
    monitors = (
        await db.execute(select(ProviderChannelMonitor).where(ProviderChannelMonitor.channel_id == channel_id))
    ).scalars().all()
    return _provider_channel_payload(
        channel,
        route_count=int(route_count or 0),
        runtime_state=runtime_state,
        monitor_selection=_provider_channel_monitor_selection_payload(channel, routes, monitors),
    )


@router.put("/provider-channels/{channel_id}/monitor-selection", dependencies=[Depends(admin_guard)])
async def update_provider_channel_monitor_selection(
    channel_id: str,
    payload: AdminProviderChannelMonitorSelectionUpdate,
    db: AsyncSession = Depends(get_db),
):
    channel = await db.scalar(
        select(ProviderChannel).where(ProviderChannel.id == channel_id).with_for_update()
    )
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider channel not found")
    routes = (
        await db.execute(select(ModelChannelRoute).where(ModelChannelRoute.channel_id == channel_id))
    ).scalars().all()
    monitors = (
        await db.execute(select(ProviderChannelMonitor).where(ProviderChannelMonitor.channel_id == channel_id))
    ).scalars().all()
    choices = _provider_channel_route_choices(channel, routes)

    try:
        selected_monitor: ProviderChannelMonitor | None = None
        if payload.mode == "manual":
            if not payload.model or not payload.endpoint:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="manual selection requires model and endpoint")
            target = (payload.endpoint.strip(), payload.model.strip())
            valid_targets = {(item["endpoint"], item["model"]) for item in choices}
            if target not in valid_targets:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="monitor selection is not an active channel route")
            matching = sorted(
                (
                    monitor
                    for monitor in monitors
                    if (
                        str(getattr(monitor, "endpoint", "") or "").strip(),
                        str(getattr(monitor, "primary_model", "") or "").strip(),
                    )
                    == target
                ),
                key=lambda monitor: (
                    getattr(monitor, "created_by", "") == AUTO_MONITOR_CREATED_BY,
                    str(getattr(monitor, "id", "") or ""),
                ),
            )
            selected_monitor = matching[0] if matching else None
            if selected_monitor is None:
                selected_monitor = ProviderChannelMonitor(
                    id=generate_id("cma_"),
                    channel_id=channel_id,
                    name=f"Manual · {channel.name} · {target[0]}"[:128],
                    endpoint=target[0],
                    primary_model=target[1],
                    extra_models=serialize_monitor_models([]),
                    status="active",
                    interval_seconds=int(_settings.provider_channel_monitor_default_interval),
                    timeout_seconds=int(_settings.provider_channel_monitor_default_timeout),
                    created_by=MANUAL_MONITOR_CREATED_BY,
                )
                db.add(selected_monitor)
                monitors.append(selected_monitor)
            selected_monitor.name = f"Manual · {channel.name} · {target[0]}"[:128]
            selected_monitor.extra_models = serialize_monitor_models([])
            selected_monitor.status = "active"
            selected_monitor.created_by = MANUAL_MONITOR_CREATED_BY
            for monitor in monitors:
                if monitor is not selected_monitor:
                    monitor.status = "disabled"
        else:
            recommended = choices[0] if choices else None
            if recommended is not None:
                target = (recommended["endpoint"], recommended["model"])
                matching = sorted(
                    (
                        monitor
                        for monitor in monitors
                        if (
                            str(getattr(monitor, "endpoint", "") or "").strip(),
                            str(getattr(monitor, "primary_model", "") or "").strip(),
                        )
                        == target
                    ),
                    key=lambda monitor: (
                        getattr(monitor, "created_by", "") != AUTO_MONITOR_CREATED_BY,
                        str(getattr(monitor, "id", "") or ""),
                    ),
                )
                selected_monitor = matching[0] if matching else None
                if selected_monitor is None:
                    selected_monitor = ProviderChannelMonitor(
                        id=generate_id("cma_"),
                        channel_id=channel_id,
                        name=f"Auto · {channel.name} · {target[0]}"[:128],
                        endpoint=target[0],
                        primary_model=target[1],
                        extra_models=serialize_monitor_models([]),
                        status="active",
                        interval_seconds=int(_settings.provider_channel_monitor_default_interval),
                        timeout_seconds=int(_settings.provider_channel_monitor_default_timeout),
                        created_by=AUTO_MONITOR_CREATED_BY,
                    )
                    db.add(selected_monitor)
                    monitors.append(selected_monitor)
            for monitor in monitors:
                monitor.created_by = AUTO_MONITOR_CREATED_BY
                monitor.status = "active" if monitor is selected_monitor else "disabled"
                monitor.extra_models = serialize_monitor_models([])

        await db.flush()
        await reconcile_provider_channel_monitors(db, commit=False)
        reconciled_monitors = (
            await db.execute(select(ProviderChannelMonitor).where(ProviderChannelMonitor.channel_id == channel_id))
        ).scalars().all()
        selection = _provider_channel_monitor_selection_payload(channel, routes, reconciled_monitors)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    invalidate_reliability_cache()
    return {"channel_id": channel_id, "monitor_selection": selection}


@router.post("/provider-channels/{channel_id}/test-connection", dependencies=[Depends(admin_guard)])
async def test_provider_channel_connection(channel_id: str, db: AsyncSession = Depends(get_db)):
    channel = await db.get(ProviderChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider channel not found")
    payload = await _provider_channel_models_payload(channel)
    return {
        "ok": payload.get("ok", False),
        "channel_id": payload.get("channel_id", channel_id),
        "channel_name": payload.get("channel_name", ""),
        "models_url": payload.get("models_url", ""),
        "recommended_base_url": payload.get("recommended_base_url", ""),
        "status_code": payload.get("status_code", 0),
        "latency_ms": payload.get("latency_ms", 0),
        "model_count": payload.get("model_count", 0),
        "sample_models": (payload.get("models") or [])[:8],
        "attempts": payload.get("attempts") or [],
        "error": payload.get("error", ""),
    }


@router.get("/provider-channels/{channel_id}/upstream-models", dependencies=[Depends(admin_guard)])
async def list_provider_channel_upstream_models(channel_id: str, db: AsyncSession = Depends(get_db)):
    channel = await db.get(ProviderChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider channel not found")
    return await _provider_channel_models_payload(channel)


@router.patch("/provider-channels/{channel_id}", dependencies=[Depends(admin_guard)])
async def update_provider_channel(channel_id: str, payload: AdminProviderChannelUpdate, db: AsyncSession = Depends(get_db)):
    if not payload.model_fields_set:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no channel fields provided")
    channel = await db.get(ProviderChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider channel not found")

    fields = payload.model_fields_set
    if "name" in fields and payload.name is not None:
        channel.name = payload.name.strip()
    if "provider_platform" in fields and payload.provider_platform is not None:
        channel.provider_platform = payload.provider_platform.strip()
    if "channel_type" in fields and payload.channel_type is not None:
        channel.channel_type = payload.channel_type.strip() or "openai_compatible"
    if "base_url" in fields and payload.base_url is not None:
        channel.base_url = payload.base_url.strip().rstrip("/")
    if "api_key" in fields and payload.api_key is not None:
        channel.encrypted_api_key = encrypt_api_key(payload.api_key.strip())
    if "auth_style" in fields and payload.auth_style is not None:
        channel.auth_style = payload.auth_style
    if "status" in fields and payload.status is not None:
        channel.status = payload.status
    if "priority" in fields and payload.priority is not None:
        channel.priority = int(payload.priority)
    if "weight" in fields and payload.weight is not None:
        channel.weight = int(payload.weight)
    if "allowed_fails" in fields and payload.allowed_fails is not None:
        channel.allowed_fails = int(payload.allowed_fails)
    if "cooldown_seconds" in fields and payload.cooldown_seconds is not None:
        channel.cooldown_seconds = float(payload.cooldown_seconds)
    if "capabilities" in fields:
        channel.capabilities = _csv_from_list(payload.capabilities or [])
    if "provider_account_fingerprint" in fields and payload.provider_account_fingerprint is not None:
        channel.provider_account_fingerprint = payload.provider_account_fingerprint.strip()
    if "cost_tier" in fields and payload.cost_tier is not None:
        channel.cost_tier = payload.cost_tier.strip()
    if "notes" in fields and payload.notes is not None:
        channel.notes = payload.notes
    channel.updated_by = "admin"
    await db.commit()
    await _refresh_reliability_after_control_plane_change(db, reconcile_monitors=True)
    await refresh_provider_channel_router_from_db(db)
    route_count = await db.scalar(select(func.count(ModelChannelRoute.id)).where(ModelChannelRoute.channel_id == channel_id)) or 0
    runtime_state = await db.get(ProviderChannelRuntimeState, channel_id)
    return _provider_channel_payload(channel, route_count=int(route_count or 0), runtime_state=runtime_state)


@router.delete("/provider-channels/{channel_id}", dependencies=[Depends(admin_guard)])
async def delete_provider_channel(channel_id: str, db: AsyncSession = Depends(get_db)):
    channel = await db.get(ProviderChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider channel not found")
    route_count = await db.scalar(select(func.count(ModelChannelRoute.id)).where(ModelChannelRoute.channel_id == channel_id)) or 0
    monitor_history_count = await db.scalar(
        select(func.count(ProviderChannelMonitorHistory.id)).where(
            ProviderChannelMonitorHistory.channel_id == channel_id
        )
    ) or 0
    monitor_rollup_count = await db.scalar(
        select(func.count(ProviderChannelMonitorDailyRollup.id)).where(
            ProviderChannelMonitorDailyRollup.channel_id == channel_id
        )
    ) or 0
    has_monitor_history = int(monitor_history_count or 0) > 0 or int(monitor_rollup_count or 0) > 0
    if int(route_count or 0) > 0 or has_monitor_history:
        channel.status = "disabled"
        channel.updated_by = "admin"
        await db.commit()
        await _refresh_reliability_after_control_plane_change(db, reconcile_monitors=True)
        await refresh_provider_channel_router_from_db(db)
        return {
            "deleted": False,
            "disabled": True,
            "reason": (
                "channel still has model routes"
                if int(route_count or 0) > 0
                else "channel has monitoring history"
            ),
            "channel": _provider_channel_payload(channel, route_count=int(route_count or 0)),
        }

    await db.execute(
        delete(ProviderChannelMonitor).where(ProviderChannelMonitor.channel_id == channel_id)
    )
    runtime_state = await db.get(ProviderChannelRuntimeState, channel_id)
    if runtime_state is not None:
        await db.delete(runtime_state)
    await db.delete(channel)
    await db.commit()
    channel_router.reset_channel_state(channel_id)
    await _refresh_reliability_after_control_plane_change(db, reconcile_monitors=True)
    await refresh_provider_channel_router_from_db(db)
    return {"deleted": True, "channel_id": channel_id}


@router.post("/provider-channels/{channel_id}/clear-cooldown", dependencies=[Depends(admin_guard)])
async def clear_provider_channel_cooldown(channel_id: str, db: AsyncSession = Depends(get_db)):
    channel = await db.get(ProviderChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider channel not found")
    channel_router.reset_channel_state(channel_id)
    runtime_state = await db.get(ProviderChannelRuntimeState, channel_id)
    if runtime_state is not None:
        runtime_state.fail_count = 0
        runtime_state.cooldown_until = None
        runtime_state.last_error_code = ""
        runtime_state.last_error_message = ""
        await db.commit()
    await _refresh_reliability_after_control_plane_change(db)
    return _provider_channel_payload(channel, runtime_state=runtime_state)


@router.get("/model-channel-routes", dependencies=[Depends(admin_guard)])
async def list_model_channel_routes(public_model_id: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    query = (
        select(ModelChannelRoute, ProviderChannel)
        .outerjoin(ProviderChannel, ProviderChannel.id == ModelChannelRoute.channel_id)
        .order_by(ModelChannelRoute.public_model_id.asc(), ModelChannelRoute.endpoint.asc(), ModelChannelRoute.created_at.desc())
    )
    if public_model_id:
        query = query.where(ModelChannelRoute.public_model_id == public_model_id)
    rows = (await db.execute(query)).all()
    return {
        "routes": [_model_channel_route_payload(route, channel) for route, channel in rows],
        "router_version": channel_router.version,
    }


@router.post("/model-channel-routes", dependencies=[Depends(admin_guard)])
async def create_model_channel_route(payload: AdminModelChannelRouteCreate, db: AsyncSession = Depends(get_db)):
    public_model_id = payload.public_model_id.strip()
    endpoint = (payload.endpoint or "").strip()
    _validate_model_channel_route(public_model_id, endpoint)
    channel = await db.get(ProviderChannel, payload.channel_id)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider channel not found")

    route = ModelChannelRoute(
        id=generate_id("mcr_"),
        public_model_id=public_model_id,
        endpoint=endpoint,
        channel_id=payload.channel_id,
        upstream_model=(payload.upstream_model or "").strip(),
        priority_override=payload.priority_override,
        weight_override=payload.weight_override,
        transform_profile=(payload.transform_profile or "openai_compatible").strip(),
        status=payload.status,
        notes=payload.notes or "",
        updated_by="admin",
    )
    db.add(route)
    await db.commit()
    await _refresh_reliability_after_control_plane_change(db, reconcile_monitors=True)
    await refresh_provider_channel_router_from_db(db)
    return _model_channel_route_payload(route, channel)


@router.patch("/model-channel-routes/{route_id}", dependencies=[Depends(admin_guard)])
async def update_model_channel_route(route_id: str, payload: AdminModelChannelRouteUpdate, db: AsyncSession = Depends(get_db)):
    if not payload.model_fields_set:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no route fields provided")
    route = await db.get(ModelChannelRoute, route_id)
    if route is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="model channel route not found")

    public_model_id = payload.public_model_id.strip() if payload.public_model_id is not None else route.public_model_id
    endpoint = payload.endpoint.strip() if payload.endpoint is not None else route.endpoint
    _validate_model_channel_route(public_model_id, endpoint)
    channel_id = payload.channel_id if payload.channel_id is not None else route.channel_id
    channel = await db.get(ProviderChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider channel not found")

    fields = payload.model_fields_set
    if "public_model_id" in fields and payload.public_model_id is not None:
        route.public_model_id = public_model_id
    if "endpoint" in fields and payload.endpoint is not None:
        route.endpoint = endpoint
    if "channel_id" in fields and payload.channel_id is not None:
        route.channel_id = payload.channel_id
    if "upstream_model" in fields and payload.upstream_model is not None:
        route.upstream_model = payload.upstream_model.strip()
    if "priority_override" in fields:
        route.priority_override = payload.priority_override
    if "weight_override" in fields:
        route.weight_override = payload.weight_override
    if "transform_profile" in fields and payload.transform_profile is not None:
        route.transform_profile = payload.transform_profile.strip() or "openai_compatible"
    if "status" in fields and payload.status is not None:
        route.status = payload.status
    if "notes" in fields and payload.notes is not None:
        route.notes = payload.notes
    route.updated_by = "admin"
    await db.commit()
    await _refresh_reliability_after_control_plane_change(db, reconcile_monitors=True)
    await refresh_provider_channel_router_from_db(db)
    return _model_channel_route_payload(route, channel)


@router.delete("/model-channel-routes/{route_id}", dependencies=[Depends(admin_guard)])
async def delete_model_channel_route(route_id: str, db: AsyncSession = Depends(get_db)):
    route = await db.get(ModelChannelRoute, route_id)
    if route is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="model channel route not found")
    await db.delete(route)
    await db.commit()
    await _refresh_reliability_after_control_plane_change(db, reconcile_monitors=True)
    await refresh_provider_channel_router_from_db(db)
    return {"deleted": True, "route_id": route_id}


@router.get("/settings/claude-compat", dependencies=[Depends(admin_guard)])
async def get_claude_compat_settings(db: AsyncSession = Depends(get_db)):
    await refresh_runtime_system_settings_from_db(db)
    return _claude_compat_settings_payload()


@router.patch("/settings/claude-compat", dependencies=[Depends(admin_guard)])
async def update_claude_compat_settings(payload: AdminClaudeCompatSettingsUpdate, db: AsyncSession = Depends(get_db)):
    provider = payload.provider.strip().lower()
    if provider not in CLAUDE_COMPAT_PROVIDERS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unsupported claude compat provider")
    if provider == CLAUDE_COMPAT_PROVIDER_KIRO_GO:
        if not str(getattr(_settings, "claude_compat_base_url", "") or "").strip():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="COINCOIN_CLAUDE_COMPAT_BASE_URL is not configured")

    existing = (
        await db.execute(select(SystemSetting).where(SystemSetting.setting_key == CLAUDE_COMPAT_PROVIDER_KEY))
    ).scalar_one_or_none()
    if existing is None:
        existing = SystemSetting(setting_key=CLAUDE_COMPAT_PROVIDER_KEY)
        db.add(existing)
    existing.setting_value = provider
    existing.updated_by = "admin"
    await db.commit()
    apply_runtime_system_setting(CLAUDE_COMPAT_PROVIDER_KEY, provider)
    return _claude_compat_settings_payload()


@router.get("/users", dependencies=[Depends(admin_guard)])
async def list_users(
    search: Optional[str] = None,
    usage_sort: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    usage_sort, usage_sort_hours = _user_usage_sort_window(usage_sort)
    usage_columns = None
    if usage_sort and usage_sort_hours:
        since = datetime.utcnow() - timedelta(hours=usage_sort_hours)
        request_charge = _request_user_charge_expr()
        usage_columns = (
            select(
                RequestLog.user_id.label("usage_user_id"),
                func.count(RequestLog.id).label("period_requests_total"),
                func.coalesce(func.sum(RequestLog.input_tokens + RequestLog.output_tokens), 0).label("period_tokens_total"),
                func.coalesce(func.sum(RequestLog.image_count), 0).label("period_images_total"),
                func.coalesce(func.sum(RequestLog.video_count), 0).label("period_videos_total"),
                func.coalesce(func.sum(request_charge), 0).label("period_cost_cents"),
            )
            .where(RequestLog.created_at >= since)
            .group_by(RequestLog.user_id)
            .subquery()
        )
    query = (
        select(User, StationCustomerLink, Station)
        .outerjoin(StationCustomerLink, StationCustomerLink.user_id == User.id)
        .outerjoin(Station, Station.id == StationCustomerLink.station_id)
    )
    if usage_columns is not None:
        query = query.add_columns(
            usage_columns.c.period_requests_total,
            usage_columns.c.period_tokens_total,
            usage_columns.c.period_images_total,
            usage_columns.c.period_videos_total,
            usage_columns.c.period_cost_cents,
        ).outerjoin(usage_columns, usage_columns.c.usage_user_id == User.id)
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
    if usage_columns is not None:
        query = query.order_by(func.coalesce(usage_columns.c.period_cost_cents, 0).desc(), User.created_at.desc())
    else:
        query = query.order_by(User.created_at.desc())
    result = await db.execute(query.limit(200))
    rows = result.all()
    billing_by_user = await _admin_billing_states_batch(db, [row[0] for row in rows])
    items = []
    for row in rows:
        u, link, station = row[:3]
        period_usage = None
        if usage_columns is not None:
            period_usage = {
                "period": usage_sort,
                "window_hours": usage_sort_hours,
                "requests_total": int((row[3] if len(row) > 3 else 0) or 0),
                "tokens_total": int((row[4] if len(row) > 4 else 0) or 0),
                "images_total": int((row[5] if len(row) > 5 else 0) or 0),
                "videos_total": int((row[6] if len(row) > 6 else 0) or 0),
                "cost_cents": int((row[7] if len(row) > 7 else 0) or 0),
            }
        billing = billing_by_user.get(u.id, {})
        items.append({
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
            **_admin_available_fields(billing),
            "billing": billing,
            "billing_summary": _billing_summary_for_admin(billing),
            "period_usage": period_usage,
            "station_attribution": None if not station else {
                "station_id": station.id,
                "station_name": station.display_name,
                "station_owner_user_id": station.owner_user_id,
                "link_status": getattr(link, "status", None),
            },
        })
    return items


@router.post(
    "/users",
    dependencies=[Depends(admin_guard)],
    response_model=AdminUserCreateResponse,
)
async def create_admin_user(
    payload: AdminUserCreateRequest,
    db: AsyncSession = Depends(get_db),
):
    username = (payload.username or "").strip() or None
    external_id = (payload.external_id or "").strip() or None
    password = (payload.password or "").strip()
    if not username and not external_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="username or external_id required")
    if password and not username:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="username required when password is provided")

    username_user = None
    if username:
        username_user = (
            await db.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
    external_user = None
    if external_id:
        external_user = (
            await db.execute(select(User).where(User.external_id == external_id))
        ).scalar_one_or_none()
    if username_user and external_user and username_user.id != external_user.id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="username and external_id belong to different users")

    user = username_user or external_user
    if not user and external_id:
        user = external_user
    if user and user.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="user blocked")
    if user and username and user.username and user.username != username:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="username belongs to a different user identity")
    if user and external_id and user.external_id and user.external_id != external_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="external_id belongs to a different user identity")

    if not user:
        user = User(
            id=generate_id("u_"),
            username=username,
            external_id=external_id,
            status="active",
            token_used=0,
            balance=_settings.default_balance,
            referral_code=generate_referral_code(),
        )
        db.add(user)
        await db.flush()
        await ensure_finance_summary_initialized(db, user.id, commit=False)
        if _settings.default_balance > 0:
            await increment_finance_summary(db, user.id, bonus_cents=_settings.default_balance)
    else:
        if username and not user.username:
            user.username = username
        if external_id and not user.external_id:
            user.external_id = external_id

    account_status = None
    if password:
        account_by_user = (
            await db.execute(select(Account).where(Account.linked_user_id == user.id))
        ).scalar_one_or_none()
        account_by_username = (
            await db.execute(select(Account).where(Account.username == username))
        ).scalar_one_or_none()
        if account_by_user and account_by_username and account_by_user.id != account_by_username.id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="username already has an account")
        if account_by_username and account_by_username.linked_user_id != user.id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="username already has an account")
        account = account_by_user or account_by_username
        if not account:
            account = Account(
                id=generate_id("acc_"),
                username=username,
                password_hash="",
                linked_user_id=user.id,
            )
            db.add(account)
        account.username = username
        account.password_hash = await hash_password(password)
        account.status = "active"
        account.failed_attempts = 0
        account.locked_until = None
        account_status = account.status

    api_key_value = None
    key = None
    if payload.create_api_key:
        api_key_value = generate_api_key()
        key = ApiKey(
            id=generate_id("k_"),
            user_id=user.id,
            key_hash=hash_key(api_key_value),
            encrypted_key=encrypt_api_key(api_key_value),
            kind="api",
            status="active",
            created_at=datetime.utcnow(),
        )
        db.add(key)

    await db.commit()

    return {
        "user_id": user.id,
        "username": user.username,
        "external_id": user.external_id,
        "api_key": api_key_value,
        "key_id": getattr(key, "id", None),
        "account_status": account_status,
        "status": user.status,
    }


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
    if "token_limit" in payload.model_fields_set:
        user.token_limit = payload.token_limit
    if payload.token_used is not None:
        user.token_used = payload.token_used
    if payload.input_tokens_used is not None:
        user.input_tokens_used = payload.input_tokens_used
    if payload.output_tokens_used is not None:
        user.output_tokens_used = payload.output_tokens_used
    if "request_limit_per_minute" in payload.model_fields_set:
        user.request_limit_per_minute = payload.request_limit_per_minute
    if "request_limit_per_day" in payload.model_fields_set:
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


@router.patch("/users/{user_id}/subscription", dependencies=[Depends(admin_guard)])
async def adjust_user_subscription(
    user_id: str,
    payload: AdminSubscriptionAdjustRequest,
    db: AsyncSession = Depends(get_db),
):
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    product = MONTHLY_BY_ID.get(payload.plan_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown monthly plan")

    now = utcnow()
    sub = await get_subscription_for_update(db, user_id)
    before_remaining = available_subscription_cents(sub)
    if sub is None:
        sub = UserSubscription(id=generate_id("sub_"), user_id=user_id)
        db.add(sub)

    period_start = _normalize_utc_naive(payload.period_start) or sub.period_start or now
    paid_until = _normalize_utc_naive(payload.paid_until) or sub.paid_until or (now + timedelta(days=30))
    period_end = _normalize_utc_naive(payload.period_end) or sub.period_end
    if not period_end:
        period_end = min(period_start + timedelta(days=30), paid_until)

    sub.plan_id = product.id
    sub.status = payload.status
    sub.period_start = period_start
    sub.period_end = period_end
    sub.paid_until = paid_until
    sub.quota_cents = product.balance_cents if payload.quota_cents is None else int(payload.quota_cents)
    if sub.status == "active":
        normalize_subscription_period(sub, now)
    sub.used_cents = min(int(payload.used_cents or 0), int(sub.quota_cents or 0)) if "used_cents" in payload.model_fields_set else min(int(sub.used_cents or 0), int(sub.quota_cents or 0))

    after_remaining = available_subscription_cents(sub)
    add_billing_ledger(
        db,
        user_id=user_id,
        entry_type="admin_subscription_adjust",
        amount_cents=after_remaining - before_remaining,
        source_type="admin",
        source_id="manual",
        product_id=product.id,
        balance_after_cents=after_remaining,
        note=payload.note or "admin adjusted subscription",
    )
    await db.commit()
    billing = await _admin_billing_state(db, user)
    return {
        "user_id": user_id,
        "billing": billing,
        "billing_summary": _billing_summary_for_admin(billing),
    }


@router.post("/users/{user_id}/traffic-packs", dependencies=[Depends(admin_guard)])
async def grant_user_traffic_pack(
    user_id: str,
    payload: AdminTrafficPackGrantRequest,
    db: AsyncSession = Depends(get_db),
):
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    product = ADDONS_BY_ID.get(payload.product_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown traffic pack")

    now = utcnow()
    remaining_cents = int(product.balance_cents if payload.remaining_cents is None else payload.remaining_cents)
    pack = TrafficPackBalance(
        id=generate_id("tp_"),
        user_id=user_id,
        product_id=product.id,
        status="active" if remaining_cents > 0 else "depleted",
        original_cents=product.balance_cents,
        remaining_cents=remaining_cents,
        expires_at=_normalize_utc_naive(payload.expires_at) or (now + timedelta(days=TRAFFIC_PACK_VALID_DAYS)),
    )
    db.add(pack)
    add_billing_ledger(
        db,
        user_id=user_id,
        entry_type="admin_traffic_pack_grant",
        amount_cents=remaining_cents,
        source_type="admin",
        source_id=pack.id,
        product_id=product.id,
        balance_after_cents=remaining_cents,
        note=payload.note or "admin granted traffic pack",
    )
    await db.commit()
    billing = await _admin_billing_state(db, user)
    return {
        "user_id": user_id,
        "traffic_pack_id": pack.id,
        "billing": billing,
        "billing_summary": _billing_summary_for_admin(billing),
    }


@router.patch("/traffic-packs/{pack_id}", dependencies=[Depends(admin_guard)])
async def update_traffic_pack(
    pack_id: str,
    payload: AdminTrafficPackUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    pack = await get_traffic_pack_for_update(db, pack_id)
    if not pack:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="traffic pack not found")
    user = (await db.execute(select(User).where(User.id == pack.user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found for traffic pack")

    before_remaining = int(pack.remaining_cents or 0)
    if payload.status is not None:
        pack.status = payload.status
    if payload.remaining_cents is not None:
        pack.remaining_cents = int(payload.remaining_cents)
        if pack.remaining_cents <= 0 and (payload.status is None or pack.status == "active"):
            pack.status = "depleted"
    if payload.expires_at is not None:
        pack.expires_at = _normalize_utc_naive(payload.expires_at)
    after_remaining = int(pack.remaining_cents or 0)

    add_billing_ledger(
        db,
        user_id=pack.user_id,
        entry_type="admin_traffic_pack_adjust",
        amount_cents=after_remaining - before_remaining,
        source_type="admin",
        source_id=pack.id,
        product_id=pack.product_id,
        balance_after_cents=after_remaining,
        note=payload.note or "admin adjusted traffic pack",
    )
    await db.commit()
    billing = await _admin_billing_state(db, user)
    return {
        "user_id": pack.user_id,
        "traffic_pack_id": pack.id,
        "billing": billing,
        "billing_summary": _billing_summary_for_admin(billing),
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


@router.post(
    "/users/{user_id}/reset-password",
    dependencies=[Depends(admin_guard)],
    response_model=AdminUserPasswordResetResponse,
)
async def reset_user_password(
    user_id: str,
    payload: AdminUserPasswordResetRequest,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    account = (
        await db.execute(select(Account).where(Account.linked_user_id == user.id))
    ).scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="account not found")

    account.password_hash = await hash_password(payload.new_password)
    account.failed_attempts = 0
    account.locked_until = None
    await db.commit()
    return {
        "user_id": user.id,
        "username": account.username,
        "account_status": account.status,
        "status": "password_reset",
    }


@router.get("/users/{user_id}", dependencies=[Depends(admin_guard)])
async def get_user_detail(user_id: str, db: AsyncSession = Depends(get_db)):
    """获取用户详情，包含该用户的所有 Key"""
    model_registry.ensure_initialized()
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
    billing = await _admin_billing_state(db, user)
    routing_rows = (
        await db.execute(
            select(UserModelRoutingOverride)
            .where(UserModelRoutingOverride.user_id == user_id)
            .order_by(UserModelRoutingOverride.public_model_id.asc())
        )
    ).scalars().all()
    pricing_rows = (
        await db.execute(
            select(UserModelPricingOverride)
            .where(UserModelPricingOverride.user_id == user_id)
            .order_by(UserModelPricingOverride.public_model_id.asc())
        )
    ).scalars().all()
    finance = _admin_finance_summary(
        await build_user_finance_snapshot(db, user.id, user.balance),
        billing,
    )
    
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
        **_admin_available_fields(billing),
        "billing": billing,
        "billing_summary": _billing_summary_for_admin(billing),
        "finance_summary": finance,
        "model_routing_overrides": [_serialize_user_routing_override(row) for row in routing_rows],
        "model_pricing_overrides": [_serialize_user_pricing_override(row) for row in pricing_rows],
        "user_model_override_options": _user_override_model_options(),
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


@router.get("/users/{user_id}/model-routing-overrides", dependencies=[Depends(admin_guard)])
async def list_user_model_routing_overrides(user_id: str, db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(UserModelRoutingOverride)
            .where(UserModelRoutingOverride.user_id == user_id)
            .order_by(UserModelRoutingOverride.public_model_id.asc())
        )
    ).scalars().all()
    return {
        "items": [_serialize_user_routing_override(row) for row in rows],
        "count": len(rows),
    }


@router.put("/users/{user_id}/model-routing-overrides/{public_model_id}", dependencies=[Depends(admin_guard)])
async def upsert_user_model_routing_override(
    user_id: str,
    public_model_id: str,
    payload: AdminUserModelRoutingOverrideUpsert,
    db: AsyncSession = Depends(get_db),
):
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    if not model_registry.get_public_model(public_model_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="model not found")

    override = {}
    if payload.target_alias:
        target = _matching_target(public_model_id, payload.target_alias.strip())
        if not target:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="target alias is not compatible")
        override["provider_model"] = target.get("provider_model") or ""
        override["upstream_model"] = target.get("upstream_model") or target.get("provider_model") or ""
    else:
        provider_model = payload.provider_model.strip() if payload.provider_model is not None else ""
        upstream_model = payload.upstream_model.strip() if payload.upstream_model is not None else ""
        if provider_model or upstream_model:
            target = _matching_target_by_models(public_model_id, provider_model, upstream_model or provider_model)
            if not target:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="target model is not compatible")
            override["provider_model"] = target.get("provider_model") or ""
            override["upstream_model"] = target.get("upstream_model") or target.get("provider_model") or ""
    if payload.enabled is not None:
        override["enabled"] = payload.enabled
    if not override:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no override fields provided")

    row = (
        await db.execute(
            select(UserModelRoutingOverride).where(
                UserModelRoutingOverride.user_id == user_id,
                UserModelRoutingOverride.public_model_id == public_model_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = UserModelRoutingOverride(user_id=user_id, public_model_id=public_model_id)
        db.add(row)
    if "provider_model" in override:
        row.provider_model = override["provider_model"]
    if "upstream_model" in override:
        row.upstream_model = override["upstream_model"]
    if "enabled" in override:
        row.enabled = 1 if override["enabled"] else 0
    row.updated_by = "admin"
    response_payload = _user_routing_override_payload(
        public_model_id=public_model_id,
        provider_model=row.provider_model,
        upstream_model=row.upstream_model,
        enabled=bool(row.enabled),
        updated_by=row.updated_by,
        updated_at=getattr(row, "updated_at", None),
    )
    await db.commit()
    await _invalidate_user_key_cache(db, user_id)
    return response_payload


@router.delete("/users/{user_id}/model-routing-overrides/{public_model_id}", dependencies=[Depends(admin_guard)])
async def delete_user_model_routing_override(
    user_id: str,
    public_model_id: str,
    db: AsyncSession = Depends(get_db),
):
    row = (
        await db.execute(
            select(UserModelRoutingOverride).where(
                UserModelRoutingOverride.user_id == user_id,
                UserModelRoutingOverride.public_model_id == public_model_id,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.commit()
        await _invalidate_user_key_cache(db, user_id)
    return {"user_id": user_id, "public_model_id": public_model_id, "deleted": row is not None}


@router.get("/users/{user_id}/model-pricing-overrides", dependencies=[Depends(admin_guard)])
async def list_user_model_pricing_overrides(user_id: str, db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(UserModelPricingOverride)
            .where(UserModelPricingOverride.user_id == user_id)
            .order_by(UserModelPricingOverride.public_model_id.asc())
        )
    ).scalars().all()
    return {
        "items": [_serialize_user_pricing_override(row) for row in rows],
        "count": len(rows),
    }


@router.put("/users/{user_id}/model-pricing-overrides/{public_model_id}", dependencies=[Depends(admin_guard)])
async def upsert_user_model_pricing_override(
    user_id: str,
    public_model_id: str,
    payload: AdminUserModelPricingOverrideUpsert,
    db: AsyncSession = Depends(get_db),
):
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    if not model_registry.get_public_model(public_model_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="model not found")

    row = (
        await db.execute(
            select(UserModelPricingOverride).where(
                UserModelPricingOverride.user_id == user_id,
                UserModelPricingOverride.public_model_id == public_model_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = UserModelPricingOverride(user_id=user_id, public_model_id=public_model_id)
        db.add(row)
    row.cache_read_multiplier_override = float(payload.cache_read_multiplier_override)
    row.updated_by = "admin"
    response_payload = _user_pricing_override_payload(
        public_model_id=public_model_id,
        cache_read_multiplier_override=row.cache_read_multiplier_override,
        updated_by=row.updated_by,
        updated_at=getattr(row, "updated_at", None),
    )
    await db.commit()
    await _invalidate_user_key_cache(db, user_id)
    return response_payload


@router.delete("/users/{user_id}/model-pricing-overrides/{public_model_id}", dependencies=[Depends(admin_guard)])
async def delete_user_model_pricing_override(
    user_id: str,
    public_model_id: str,
    db: AsyncSession = Depends(get_db),
):
    row = (
        await db.execute(
            select(UserModelPricingOverride).where(
                UserModelPricingOverride.user_id == user_id,
                UserModelPricingOverride.public_model_id == public_model_id,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.commit()
        await _invalidate_user_key_cache(db, user_id)
    return {"user_id": user_id, "public_model_id": public_model_id, "deleted": row is not None}


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
            "videos_total": getattr(usage, "videos_total", 0),
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
    total_videos_today = await db.scalar(
        select(func.coalesce(func.sum(UsageDaily.videos_total), 0)).where(UsageDaily.day == today)
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
        "total_videos_today": int(total_videos_today or 0),
        "paid_today_cents": int(paid_today_cents or 0),
        "paid_today_usd": int(paid_today_cents or 0) / 100,
        "consumed_today_cents": int(consumed_today_cents or 0),
        "consumed_today_usd": int(consumed_today_cents or 0) / 100,
        "net_today_cents": int((paid_today_cents or 0) - (consumed_today_cents or 0)),
        "net_today_usd": int((paid_today_cents or 0) - (consumed_today_cents or 0)) / 100,
    }


@router.get("/analytics/overview", dependencies=[Depends(admin_guard)])
async def analytics_overview(period: str = "today", db: AsyncSession = Depends(get_db)):
    period, days, start_day, since = _analytics_period(period)
    end_at = datetime.utcnow()
    total_users = await db.scalar(select(func.count()).select_from(User))
    active_users = await db.scalar(select(func.count()).select_from(User).where(User.status == "active"))
    positive_balance_users = await _positive_balance_users_count(db)
    paid_cents = await db.scalar(
        select(func.coalesce(func.sum(PaymentOrder.add_balance_cents), 0)).where(
            PaymentOrder.status == "confirmed",
            PaymentOrder.confirmed_at >= (since if period == "today" else _period_start_datetime(start_day)),
        )
    )
    if period == "today":
        request_charge = _request_user_charge_expr()
        usage_row = (
            await db.execute(
                select(
                    func.count(func.distinct(RequestLog.user_id)).label("active_users"),
                    func.count(RequestLog.id).label("requests_total"),
                    func.coalesce(func.sum(RequestLog.input_tokens), 0).label("input_tokens"),
                    func.coalesce(func.sum(RequestLog.output_tokens), 0).label("output_tokens"),
                    func.coalesce(func.sum(RequestLog.input_tokens + RequestLog.output_tokens), 0).label("tokens_total"),
                    func.coalesce(func.sum(RequestLog.image_count), 0).label("images_total"),
                    func.coalesce(func.sum(RequestLog.video_count), 0).label("videos_total"),
                    func.coalesce(func.sum(request_charge), 0).label("cost_cents"),
                ).where(RequestLog.created_at >= since)
            )
        ).first()
    else:
        usage_row = (
            await db.execute(
                select(
                    func.count(func.distinct(UsageDaily.user_id)).label("active_users"),
                    func.coalesce(func.sum(UsageDaily.requests_total), 0).label("requests_total"),
                    func.coalesce(func.sum(UsageDaily.input_tokens), 0).label("input_tokens"),
                    func.coalesce(func.sum(UsageDaily.output_tokens), 0).label("output_tokens"),
                    func.coalesce(func.sum(UsageDaily.tokens_total), 0).label("tokens_total"),
                    func.coalesce(func.sum(UsageDaily.images_total), 0).label("images_total"),
                    func.coalesce(func.sum(UsageDaily.videos_total), 0).label("videos_total"),
                    func.coalesce(func.sum(UsageDaily.cost_cents), 0).label("cost_cents"),
                ).where(UsageDaily.day >= start_day)
            )
        ).first()

    user_charge_cents = int(_row_value(usage_row, "cost_cents", 0) or 0)
    paid_cents = int(paid_cents or 0)
    return {
        "period": period,
        "days": days,
        **_analytics_period_fields(period, days, start_day, since, end_at=end_at),
        "total_users": int(total_users or 0),
        "active_users": int(active_users or 0),
        "positive_balance_users": positive_balance_users,
        "users_with_balance": positive_balance_users,
        "active_users_period": int(_row_value(usage_row, "active_users", 0) or 0),
        "requests_total": int(_row_value(usage_row, "requests_total", 0) or 0),
        "input_tokens": int(_row_value(usage_row, "input_tokens", 0) or 0),
        "output_tokens": int(_row_value(usage_row, "output_tokens", 0) or 0),
        "tokens_total": int(_row_value(usage_row, "tokens_total", 0) or 0),
        "images_total": int(_row_value(usage_row, "images_total", 0) or 0),
        "videos_total": int(_row_value(usage_row, "videos_total", 0) or 0),
        "user_charge_cents": user_charge_cents,
        "user_charge_usd": user_charge_cents / 100,
        "paid_cents": paid_cents,
        "paid_usd": paid_cents / 100,
        "net_cashflow_cents": paid_cents - user_charge_cents,
        "net_cashflow_usd": (paid_cents - user_charge_cents) / 100,
    }


@router.get("/analytics/top-users", dependencies=[Depends(admin_guard)])
async def analytics_top_users(
    period: str = "today",
    metric: str = "cost_cents",
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
):
    period, days, start_day, since = _analytics_period(period)
    limit = max(1, min(limit, 100))
    if period == "today":
        request_charge = _request_user_charge_expr()
        metric_map = {
            "cost_cents": func.coalesce(func.sum(request_charge), 0),
            "requests_total": func.count(RequestLog.id),
            "tokens_total": func.coalesce(func.sum(RequestLog.input_tokens + RequestLog.output_tokens), 0),
            "images_total": func.coalesce(func.sum(RequestLog.image_count), 0),
            "videos_total": func.coalesce(func.sum(RequestLog.video_count), 0),
        }
        order_metric = metric_map.get(metric)
        if order_metric is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unsupported metric")
        result = await db.execute(
            select(
                User.id.label("user_id"),
                User.username.label("username"),
                User.email.label("email"),
                User.external_id.label("external_id"),
                User.balance.label("balance"),
                func.count(RequestLog.id).label("requests_total"),
                func.coalesce(func.sum(RequestLog.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(RequestLog.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(RequestLog.input_tokens + RequestLog.output_tokens), 0).label("tokens_total"),
                func.coalesce(func.sum(RequestLog.image_count), 0).label("images_total"),
                func.coalesce(func.sum(RequestLog.video_count), 0).label("videos_total"),
                func.coalesce(func.sum(request_charge), 0).label("cost_cents"),
            )
            .join(User, RequestLog.user_id == User.id)
            .where(RequestLog.created_at >= since)
            .group_by(User.id, User.username, User.email, User.external_id, User.balance)
            .order_by(order_metric.desc())
            .limit(limit)
        )
    else:
        metric_map = {
            "cost_cents": func.coalesce(func.sum(UsageDaily.cost_cents), 0),
            "requests_total": func.coalesce(func.sum(UsageDaily.requests_total), 0),
            "tokens_total": func.coalesce(func.sum(UsageDaily.tokens_total), 0),
            "images_total": func.coalesce(func.sum(UsageDaily.images_total), 0),
            "videos_total": func.coalesce(func.sum(UsageDaily.videos_total), 0),
        }
        order_metric = metric_map.get(metric)
        if order_metric is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unsupported metric")
        result = await db.execute(
            select(
                User.id.label("user_id"),
                User.username.label("username"),
                User.email.label("email"),
                User.external_id.label("external_id"),
                User.balance.label("balance"),
                func.coalesce(func.sum(UsageDaily.requests_total), 0).label("requests_total"),
                func.coalesce(func.sum(UsageDaily.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(UsageDaily.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(UsageDaily.tokens_total), 0).label("tokens_total"),
                func.coalesce(func.sum(UsageDaily.images_total), 0).label("images_total"),
                func.coalesce(func.sum(UsageDaily.videos_total), 0).label("videos_total"),
                func.coalesce(func.sum(UsageDaily.cost_cents), 0).label("cost_cents"),
            )
            .join(User, UsageDaily.user_id == User.id)
            .where(UsageDaily.day >= start_day)
            .group_by(User.id, User.username, User.email, User.external_id, User.balance)
            .order_by(order_metric.desc())
            .limit(limit)
        )
    rows = result.all()
    return {
        "period": period,
        "days": days,
        **_analytics_period_fields(period, days, start_day, since),
        "metric": metric,
        "limit": limit,
        "data": [
            {
                "rank": idx + 1,
                "user_id": _row_value(row, "user_id", ""),
                "username": _row_value(row, "username", None),
                "email": _row_value(row, "email", None),
                "external_id": _row_value(row, "external_id", None),
                "display_name": _display_name(
                    _row_value(row, "username", None),
                    _row_value(row, "email", None),
                    _row_value(row, "external_id", None),
                    _row_value(row, "user_id", ""),
                ),
                "balance_cents": int(_row_value(row, "balance", 0) or 0),
                "requests_total": int(_row_value(row, "requests_total", 0) or 0),
                "input_tokens": int(_row_value(row, "input_tokens", 0) or 0),
                "output_tokens": int(_row_value(row, "output_tokens", 0) or 0),
                "tokens_total": int(_row_value(row, "tokens_total", 0) or 0),
                "images_total": int(_row_value(row, "images_total", 0) or 0),
                "videos_total": int(_row_value(row, "videos_total", 0) or 0),
                "cost_cents": int(_row_value(row, "cost_cents", 0) or 0),
            }
            for idx, row in enumerate(rows)
        ],
    }


@router.get("/usage/leaderboard", dependencies=[Depends(admin_guard)])
async def usage_leaderboard(
    window: str = "1h",
    metric: str = "cost_cents",
    limit: int = 10,
    db: AsyncSession = Depends(get_db),
):
    window, hours = _leaderboard_window(window)
    limit = max(1, min(limit, 50))
    end_at = datetime.utcnow()
    since = end_at - timedelta(hours=hours)
    request_charge = _request_user_charge_expr()
    metric_map = {
        "cost_cents": func.coalesce(func.sum(request_charge), 0),
        "requests_total": func.count(RequestLog.id),
        "tokens_total": func.coalesce(func.sum(RequestLog.input_tokens + RequestLog.output_tokens), 0),
        "images_total": func.coalesce(func.sum(RequestLog.image_count), 0),
        "videos_total": func.coalesce(func.sum(RequestLog.video_count), 0),
    }
    order_metric = metric_map.get(metric)
    if order_metric is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unsupported metric")
    result = await db.execute(
        select(
            User.id.label("user_id"),
            User.username.label("username"),
            User.email.label("email"),
            User.external_id.label("external_id"),
            User.balance.label("balance"),
            func.count(RequestLog.id).label("requests_total"),
            func.coalesce(func.sum(RequestLog.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(RequestLog.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(RequestLog.input_tokens + RequestLog.output_tokens), 0).label("tokens_total"),
            func.coalesce(func.sum(RequestLog.image_count), 0).label("images_total"),
            func.coalesce(func.sum(RequestLog.video_count), 0).label("videos_total"),
            func.coalesce(func.sum(request_charge), 0).label("cost_cents"),
        )
        .join(User, RequestLog.user_id == User.id)
        .where(RequestLog.created_at >= since)
        .group_by(User.id, User.username, User.email, User.external_id, User.balance)
        .order_by(order_metric.desc())
        .limit(limit)
    )
    rows = result.all()
    return {
        "window": window,
        "window_hours": hours,
        "window_label": f"近 {hours} 小时" if hours < 24 else "近 24 小时",
        "window_start": since,
        "window_end": end_at,
        "metric": metric,
        "limit": limit,
        "data": [
            {
                "rank": idx + 1,
                "user_id": _row_value(row, "user_id", ""),
                "username": _row_value(row, "username", None),
                "email": _row_value(row, "email", None),
                "external_id": _row_value(row, "external_id", None),
                "display_name": _display_name(
                    _row_value(row, "username", None),
                    _row_value(row, "email", None),
                    _row_value(row, "external_id", None),
                    _row_value(row, "user_id", ""),
                ),
                "balance_cents": int(_row_value(row, "balance", 0) or 0),
                "requests_total": int(_row_value(row, "requests_total", 0) or 0),
                "input_tokens": int(_row_value(row, "input_tokens", 0) or 0),
                "output_tokens": int(_row_value(row, "output_tokens", 0) or 0),
                "tokens_total": int(_row_value(row, "tokens_total", 0) or 0),
                "images_total": int(_row_value(row, "images_total", 0) or 0),
                "videos_total": int(_row_value(row, "videos_total", 0) or 0),
                "cost_cents": int(_row_value(row, "cost_cents", 0) or 0),
            }
            for idx, row in enumerate(rows)
        ],
    }


@router.get("/analytics/low-balance-users", dependencies=[Depends(admin_guard)])
async def analytics_low_balance_users(
    period: str = "7d",
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
):
    period, days, start_day, _since = _analytics_period(period)
    limit = max(1, min(limit, 100))
    avg_daily = func.coalesce(func.sum(UsageDaily.cost_cents), 0) / days
    result = await db.execute(
        select(
            User.id.label("user_id"),
            User.username.label("username"),
            User.email.label("email"),
            User.external_id.label("external_id"),
            User.balance.label("balance"),
            func.coalesce(func.sum(UsageDaily.requests_total), 0).label("requests_total"),
            func.coalesce(func.sum(UsageDaily.tokens_total), 0).label("tokens_total"),
            func.coalesce(func.sum(UsageDaily.images_total), 0).label("images_total"),
            func.coalesce(func.sum(UsageDaily.videos_total), 0).label("videos_total"),
            func.coalesce(func.sum(UsageDaily.cost_cents), 0).label("cost_cents"),
        )
        .join(User, UsageDaily.user_id == User.id)
        .where(UsageDaily.day >= start_day, User.status == "active")
        .group_by(User.id, User.username, User.email, User.external_id, User.balance)
        .having(func.coalesce(func.sum(UsageDaily.cost_cents), 0) > 0)
        .order_by((User.balance / avg_daily).asc())
        .limit(limit)
    )
    rows = result.all()
    items = []
    for idx, row in enumerate(rows):
        balance_cents = int(_row_value(row, "balance", 0) or 0)
        cost_cents = int(_row_value(row, "cost_cents", 0) or 0)
        avg_daily_cost = int(round(cost_cents / days)) if days else 0
        days_remaining = round(balance_cents / avg_daily_cost, 2) if avg_daily_cost > 0 else None
        user_id = _row_value(row, "user_id", "")
        items.append({
            "rank": idx + 1,
            "user_id": user_id,
            "username": _row_value(row, "username", None),
            "email": _row_value(row, "email", None),
            "external_id": _row_value(row, "external_id", None),
            "display_name": _display_name(
                _row_value(row, "username", None),
                _row_value(row, "email", None),
                _row_value(row, "external_id", None),
                user_id,
            ),
            "balance_cents": balance_cents,
            "period_cost_cents": cost_cents,
            "avg_daily_cost_cents": avg_daily_cost,
            "estimated_days_remaining": days_remaining,
            "risk_level": _risk_level(days_remaining, balance_cents),
            "requests_total": int(_row_value(row, "requests_total", 0) or 0),
            "tokens_total": int(_row_value(row, "tokens_total", 0) or 0),
            "images_total": int(_row_value(row, "images_total", 0) or 0),
            "videos_total": int(_row_value(row, "videos_total", 0) or 0),
        })
    return {"period": period, "days": days, "limit": limit, "data": items}


@router.get("/analytics/errors", dependencies=[Depends(admin_guard)])
async def analytics_errors(
    period: str = "today",
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
):
    period, days, _start_day, since = _analytics_period(period)
    limit = max(1, min(limit, 100))
    total_requests = (
        await db.scalar(select(func.count()).select_from(RequestLog).where(RequestLog.created_at >= since))
    ) or 0
    failed_requests = (
        await db.scalar(
            select(func.count()).select_from(RequestLog).where(RequestLog.created_at >= since, RequestLog.status_code >= 400)
        )
    ) or 0
    status_rows = (
        await db.execute(
            select(RequestLog.status_code, func.count())
            .where(RequestLog.created_at >= since, RequestLog.status_code >= 400)
            .group_by(RequestLog.status_code)
            .order_by(func.count().desc())
            .limit(10)
        )
    ).all()
    model_rows = (
        await db.execute(
            select(RequestLog.model, func.count())
            .where(RequestLog.created_at >= since, RequestLog.status_code >= 400)
            .group_by(RequestLog.model)
            .order_by(func.count().desc())
            .limit(10)
        )
    ).all()
    recent_rows = (
        await db.execute(
            select(RequestLog, User)
            .join(User, RequestLog.user_id == User.id)
            .where(RequestLog.created_at >= since, RequestLog.status_code >= 400)
            .order_by(RequestLog.created_at.desc())
            .limit(limit)
        )
    ).all()
    total_requests = int(total_requests or 0)
    failed_requests = int(failed_requests or 0)
    return {
        "period": period,
        "days": days,
        "total_requests": total_requests,
        "failed_requests": failed_requests,
        "error_rate": (failed_requests / total_requests) if total_requests else 0,
        "by_status": [{"status_code": int(code), "count": int(count)} for code, count in status_rows],
        "by_model": [{"model": model or "-", "count": int(count)} for model, count in model_rows],
        "recent": [
            {
                "created_at": log.created_at,
                "user_id": log.user_id,
                "user": _display_name(user.username, getattr(user, "email", None), user.external_id, user.id),
                "endpoint": log.endpoint,
                "model": log.model,
                "status_code": log.status_code,
                "duration_ms": log.duration_ms,
                "route_reason": log.route_reason,
                "upstream_request_id": log.upstream_request_id,
            }
            for log, user in recent_rows
        ],
    }


@router.get("/analytics/growth", dependencies=[Depends(admin_guard)])
async def analytics_growth(period: str = "today", db: AsyncSession = Depends(get_db)):
    period, days, start_day, since = _analytics_period(period)
    end_at = datetime.utcnow()
    start_dt = since if period == "today" else _period_start_datetime(start_day)
    today_day = date.today()
    seven_days_ago = today_day - timedelta(days=7)
    new_users = await db.scalar(select(func.count(User.id)).where(User.created_at >= start_dt))
    new_api_key_users = await db.scalar(
        select(func.count(func.distinct(ApiKey.user_id))).where(ApiKey.created_at >= start_dt)
    )
    first_call_users = await db.scalar(
        select(func.count(func.distinct(RequestLog.user_id))).where(RequestLog.created_at >= start_dt)
    )
    first_paid_users = await db.scalar(
        select(func.count(func.distinct(PaymentOrder.user_id))).where(
            PaymentOrder.status == "confirmed",
            PaymentOrder.confirmed_at >= start_dt,
        )
    )
    if period == "today":
        request_charge = _request_user_charge_expr()
        daily_usage_rows = (
            await db.execute(
                select(
                    func.date(RequestLog.created_at).label("day"),
                    func.count(func.distinct(RequestLog.user_id)).label("active_users"),
                    func.count(RequestLog.id).label("requests_total"),
                    func.coalesce(func.sum(request_charge), 0).label("user_charge_cents"),
                )
                .where(RequestLog.created_at >= start_dt)
                .group_by(func.date(RequestLog.created_at))
                .order_by(func.date(RequestLog.created_at))
            )
        ).all()
    else:
        daily_usage_rows = (
            await db.execute(
                select(
                    UsageDaily.day.label("day"),
                    func.count(func.distinct(UsageDaily.user_id)).label("active_users"),
                    func.coalesce(func.sum(UsageDaily.requests_total), 0).label("requests_total"),
                    func.coalesce(func.sum(UsageDaily.cost_cents), 0).label("user_charge_cents"),
                )
                .where(UsageDaily.day >= start_day)
                .group_by(UsageDaily.day)
                .order_by(UsageDaily.day)
            )
        ).all()
    new_user_rows = (
        await db.execute(
            select(func.date(User.created_at).label("day"), func.count(User.id).label("new_users"))
            .where(User.created_at >= start_dt)
            .group_by(func.date(User.created_at))
            .order_by(func.date(User.created_at))
        )
    ).all()
    first_key_rows = (
        await db.execute(
            select(func.date(ApiKey.created_at).label("day"), func.count(func.distinct(ApiKey.user_id)).label("new_api_key_users"))
            .where(ApiKey.created_at >= start_dt)
            .group_by(func.date(ApiKey.created_at))
            .order_by(func.date(ApiKey.created_at))
        )
    ).all()
    first_call_rows = (
        await db.execute(
            select(func.date(RequestLog.created_at).label("day"), func.count(func.distinct(RequestLog.user_id)).label("first_call_users"))
            .where(RequestLog.created_at >= start_dt)
            .group_by(func.date(RequestLog.created_at))
            .order_by(func.date(RequestLog.created_at))
        )
    ).all()
    first_paid_rows = (
        await db.execute(
            select(func.date(PaymentOrder.confirmed_at).label("day"), func.count(func.distinct(PaymentOrder.user_id)).label("first_paid_users"))
            .where(PaymentOrder.status == "confirmed", PaymentOrder.confirmed_at >= start_dt)
            .group_by(func.date(PaymentOrder.confirmed_at))
            .order_by(func.date(PaymentOrder.confirmed_at))
        )
    ).all()
    day_map: dict[str, dict] = {}
    for offset in range(days):
        day = (start_day + timedelta(days=offset)).isoformat()
        day_map[day] = {
            "day": day,
            "new_users": 0,
            "new_api_key_users": 0,
            "first_call_users": 0,
            "first_paid_users": 0,
            "active_users": 0,
            "requests_total": 0,
            "user_charge_cents": 0,
        }
    for row in daily_usage_rows:
        item = day_map.setdefault(_date_key(_row_value(row, "day")), {"day": _date_key(_row_value(row, "day"))})
        item.update({
            "active_users": int(_row_value(row, "active_users", 0) or 0),
            "requests_total": int(_row_value(row, "requests_total", 0) or 0),
            "user_charge_cents": int(_row_value(row, "user_charge_cents", 0) or 0),
        })
    for rows, key in (
        (new_user_rows, "new_users"),
        (first_key_rows, "new_api_key_users"),
        (first_call_rows, "first_call_users"),
        (first_paid_rows, "first_paid_users"),
    ):
        for row in rows:
            item = day_map.setdefault(_date_key(_row_value(row, "day")), {"day": _date_key(_row_value(row, "day"))})
            item[key] = int(_row_value(row, key, 0) or 0)

    cohort_users = (
        await db.scalar(
            select(func.count(User.id)).where(
                func.date(User.created_at) == seven_days_ago,
            )
        )
    ) or 0
    retained_users = (
        await db.scalar(
            select(func.count(func.distinct(UsageDaily.user_id)))
            .join(User, UsageDaily.user_id == User.id)
            .where(func.date(User.created_at) == seven_days_ago, UsageDaily.day == today_day)
        )
    ) or 0
    return {
        "period": period,
        "days": days,
        **_analytics_period_fields(period, days, start_day, since, end_at=end_at),
        "new_users": int(new_users or 0),
        "new_positive_balance_users": None,
        "positive_balance_users": await _positive_balance_users_count(db),
        "first_paid_users": int(first_paid_users or 0),
        "new_api_key_users": int(new_api_key_users or 0),
        "first_call_users": int(first_call_users or 0),
        "retention_7d": _safe_rate(retained_users, cohort_users),
        "retention_7d_cohort_users": int(cohort_users),
        "retention_7d_retained_users": int(retained_users),
        "paid_retention_7d": None,
        "daily": [day_map[key] for key in sorted(day_map.keys())],
        "field_notes": {
            "new_positive_balance_users": "需要记录用户首次余额 > 0 的时间；当前只能展示当前有余额用户。",
            "first_call_users": "MVP 为周期内有调用的用户数；严格首次调用需预聚合 first_call_at。",
            "paid_retention_7d": "需要付费 cohort 快照；当前 MVP 暂不计算。",
        },
    }


@router.get("/analytics/revenue-margin", dependencies=[Depends(admin_guard)])
async def analytics_revenue_margin(period: str = "today", db: AsyncSession = Depends(get_db)):
    period, days, start_day, since = _analytics_period(period)
    end_at = datetime.utcnow()
    paid_since = since if period == "today" else _period_start_datetime(start_day)
    request_charge = _request_user_charge_expr()
    upstream_cost = _request_upstream_cost_expr()
    paid_rows = (
        await db.execute(
            select(
                func.date(PaymentOrder.confirmed_at).label("day"),
                func.coalesce(func.sum(PaymentOrder.add_balance_cents), 0).label("paid_cents"),
                func.count(func.distinct(PaymentOrder.user_id)).label("paid_users"),
            )
            .where(PaymentOrder.status == "confirmed", PaymentOrder.confirmed_at >= paid_since)
            .group_by(func.date(PaymentOrder.confirmed_at))
            .order_by(func.date(PaymentOrder.confirmed_at))
        )
    ).all()
    request_rows = (
        await db.execute(
            select(
                func.date(RequestLog.created_at).label("day"),
                func.coalesce(func.sum(request_charge), 0).label("user_charge_cents"),
                func.coalesce(func.sum(upstream_cost), 0).label("upstream_cost_cents"),
                func.count(RequestLog.id).label("requests_total"),
            )
            .where(RequestLog.created_at >= since)
            .group_by(func.date(RequestLog.created_at))
            .order_by(func.date(RequestLog.created_at))
        )
    ).all()
    package_consumption_cents = (
        await db.scalar(
            select(func.coalesce(func.sum(-BillingLedgerEntry.amount_cents), 0)).where(
                BillingLedgerEntry.created_at >= since,
                BillingLedgerEntry.entry_type.in_(_billing_package_entry_types()),
            )
        )
    ) or 0
    failed_payment_cents = (
        await db.scalar(
            select(func.coalesce(func.sum(PaymentOrder.add_balance_cents), 0)).where(
                PaymentOrder.created_at >= since,
                PaymentOrder.status != "confirmed",
            )
        )
    ) or 0
    def empty_revenue_day(day: str) -> dict:
        return {
            "day": day,
            "paid_cents": 0,
            "paid_users": 0,
            "user_charge_cents": 0,
            "upstream_cost_cents": 0,
            "gross_margin_cents": 0,
            "gross_margin_rate": 0,
            "requests_total": 0,
        }

    day_map: dict[str, dict] = {}
    for offset in range(days):
        day = (start_day + timedelta(days=offset)).isoformat()
        day_map[day] = empty_revenue_day(day)
    for row in paid_rows:
        day = _date_key(_row_value(row, "day"))
        item = day_map.setdefault(day, empty_revenue_day(day))
        item["paid_cents"] = int(_row_value(row, "paid_cents", 0) or 0)
        item["paid_users"] = int(_row_value(row, "paid_users", 0) or 0)
    for row in request_rows:
        day = _date_key(_row_value(row, "day"))
        item = day_map.setdefault(day, empty_revenue_day(day))
        charge = int(_row_value(row, "user_charge_cents", 0) or 0)
        cost = int(_row_value(row, "upstream_cost_cents", 0) or 0)
        item.update({
            "user_charge_cents": charge,
            "upstream_cost_cents": cost,
            "gross_margin_cents": charge - cost,
            "gross_margin_rate": _safe_rate(charge - cost, charge),
            "requests_total": int(_row_value(row, "requests_total", 0) or 0),
        })
    daily = [day_map[key] for key in sorted(day_map.keys())]
    paid_cents = sum(item["paid_cents"] for item in daily)
    user_charge_cents = sum(item["user_charge_cents"] for item in daily)
    upstream_cost_cents = sum(item["upstream_cost_cents"] for item in daily)
    gross_margin_cents = user_charge_cents - upstream_cost_cents
    return {
        "period": period,
        "days": days,
        **_analytics_period_fields(period, days, start_day, since, end_at=end_at),
        "paid_cents": paid_cents,
        "user_charge_cents": user_charge_cents,
        "upstream_cost_cents": upstream_cost_cents,
        "gross_margin_cents": gross_margin_cents,
        "gross_margin_rate": _safe_rate(gross_margin_cents, user_charge_cents),
        "package_consumption_cents": int(package_consumption_cents),
        "refund_cents": 0,
        "failed_payment_cents": int(failed_payment_cents),
        "daily": daily,
        "source_quality": _source_quality(
            upstream_cost_cents=upstream_cost_cents,
            user_charge_cents=user_charge_cents,
        ),
    }


@router.get("/analytics/usage-structure", dependencies=[Depends(admin_guard)])
async def analytics_usage_structure(
    period: str = "today",
    limit: int = 12,
    db: AsyncSession = Depends(get_db),
):
    period, days, _start_day, since = _analytics_period(period)
    limit = max(1, min(limit, 50))
    request_charge = _request_user_charge_expr()
    upstream_cost = _request_upstream_cost_expr()
    rows = (
        await db.execute(
            select(
                RequestLog.model.label("model"),
                RequestLog.billable_sku.label("billable_sku"),
                RequestLog.usage_unit_type.label("task_type"),
                func.count(RequestLog.id).label("requests"),
                func.coalesce(func.sum(RequestLog.input_tokens + RequestLog.output_tokens), 0).label("tokens"),
                func.coalesce(func.sum(RequestLog.image_count), 0).label("images"),
                func.coalesce(func.sum(RequestLog.video_count), 0).label("videos"),
                func.coalesce(func.sum(request_charge), 0).label("user_charge_cents"),
                func.coalesce(func.sum(upstream_cost), 0).label("upstream_cost_cents"),
                func.coalesce(func.avg(RequestLog.duration_ms), 0).label("avg_latency_ms"),
                func.coalesce(func.sum(case((RequestLog.status_code >= 400, 1), else_=0)), 0).label("failed_requests"),
            )
            .where(RequestLog.created_at >= since)
            .group_by(RequestLog.model, RequestLog.billable_sku, RequestLog.usage_unit_type)
            .order_by(func.coalesce(func.sum(request_charge), 0).desc())
            .limit(limit)
        )
    ).all()
    total_requests = sum(int(_row_value(row, "requests", 0) or 0) for row in rows)
    data = []
    for row in rows:
        requests = int(_row_value(row, "requests", 0) or 0)
        failed = int(_row_value(row, "failed_requests", 0) or 0)
        charge = int(_row_value(row, "user_charge_cents", 0) or 0)
        cost = int(_row_value(row, "upstream_cost_cents", 0) or 0)
        data.append({
            "model": _row_value(row, "model", "") or "-",
            "billable_sku": _row_value(row, "billable_sku", "") or (_row_value(row, "model", "") or "-"),
            "task_type": _row_value(row, "task_type", "") or "tokens",
            "requests": requests,
            "request_share": _safe_rate(requests, total_requests),
            "tokens": int(_row_value(row, "tokens", 0) or 0),
            "images": int(_row_value(row, "images", 0) or 0),
            "videos": int(_row_value(row, "videos", 0) or 0),
            "user_charge_cents": charge,
            "upstream_cost_cents": cost,
            "gross_margin_cents": charge - cost,
            "gross_margin_rate": _safe_rate(charge - cost, charge),
            "avg_latency_ms": int(float(_row_value(row, "avg_latency_ms", 0) or 0)),
            "p95_latency_ms": None,
            "failed_requests": failed,
            "failure_rate": _safe_rate(failed, requests),
            "growth_rate_7d": None,
        })
    return {
        "period": period,
        "days": days,
        "limit": limit,
        "data": data,
        "field_notes": {
            "p95_latency_ms": "MySQL 版本未统一，MVP 先返回 avg latency；后续用窗口函数或预聚合补 P95。",
            "growth_rate_7d": "需要上一窗口同维度对比；v2.1 补。",
        },
    }


@router.get("/model-latency-diagnostics", dependencies=[Depends(admin_guard)])
async def analytics_model_latency_diagnostics(
    model: str,
    period: str = "today",
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
):
    period, days, start_day, since = _analytics_period(period)
    model = (model or "").strip()
    if not model:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="model is required")
    limit = max(1, min(limit, 100))
    match_condition = (
        (RequestLog.model == model)
        | (RequestLog.provider_model == model)
        | (RequestLog.customer_model_alias == model)
        | (RequestLog.billable_sku == model)
    )
    filtered = (RequestLog.created_at >= since, match_condition)

    total = int(
        await db.scalar(
            select(func.count()).select_from(RequestLog).where(*filtered)
        )
        or 0
    )
    failed = int(
        await db.scalar(
            select(func.count()).select_from(RequestLog).where(*filtered, RequestLog.status_code >= 400)
        )
        or 0
    )
    summary = (
        await db.execute(
            select(
                func.coalesce(func.min(RequestLog.duration_ms), 0).label("min_latency_ms"),
                func.coalesce(func.avg(RequestLog.duration_ms), 0).label("avg_latency_ms"),
                func.coalesce(func.max(RequestLog.duration_ms), 0).label("max_latency_ms"),
                func.coalesce(func.sum(RequestLog.input_tokens + RequestLog.output_tokens), 0).label("tokens"),
                func.coalesce(func.sum(RequestLog.cost_cents), 0).label("user_charge_cents"),
                func.coalesce(func.sum(case((RequestLog.route_attempt > 0, 1), else_=0)), 0).label("fallback_requests"),
            ).where(*filtered)
        )
    ).first()

    latency_points = [
        int(row[0] or 0)
        for row in (
            await db.execute(
                select(RequestLog.duration_ms)
                .where(*filtered)
                .order_by(RequestLog.duration_ms.asc())
            )
        ).all()
    ]

    def percentile(values: list[int], pct: float) -> Optional[int]:
        if not values:
            return None
        index = min(len(values) - 1, max(0, int(round((len(values) - 1) * pct))))
        return values[index]

    slow_thresholds = [
        ("ge_12s", 12000),
        ("ge_30s", 30000),
        ("ge_60s", 60000),
    ]
    slow_counts = {
        key: int(
            await db.scalar(
                select(func.count()).select_from(RequestLog).where(*filtered, RequestLog.duration_ms >= threshold)
            )
            or 0
        )
        for key, threshold in slow_thresholds
    }

    user_rows = (
        await db.execute(
            select(
                RequestLog.user_id.label("user_id"),
                User.username.label("username"),
                User.email.label("email"),
                User.external_id.label("external_id"),
                func.count(RequestLog.id).label("requests"),
                func.coalesce(func.avg(RequestLog.duration_ms), 0).label("avg_latency_ms"),
                func.coalesce(func.max(RequestLog.duration_ms), 0).label("max_latency_ms"),
                func.coalesce(func.sum(RequestLog.cost_cents), 0).label("user_charge_cents"),
            )
            .join(User, RequestLog.user_id == User.id)
            .where(*filtered)
            .group_by(RequestLog.user_id, User.username, User.email, User.external_id)
            .order_by(func.avg(RequestLog.duration_ms).desc())
            .limit(limit)
        )
    ).all()

    route_rows = (
        await db.execute(
            select(
                RequestLog.route_reason.label("route_reason"),
                func.count(RequestLog.id).label("requests"),
                func.coalesce(func.avg(RequestLog.duration_ms), 0).label("avg_latency_ms"),
                func.coalesce(func.max(RequestLog.duration_ms), 0).label("max_latency_ms"),
                func.coalesce(func.sum(case((RequestLog.status_code >= 400, 1), else_=0)), 0).label("failed_requests"),
                func.coalesce(func.sum(case((RequestLog.route_attempt > 0, 1), else_=0)), 0).label("fallback_requests"),
            )
            .where(*filtered)
            .group_by(RequestLog.route_reason)
            .order_by(func.count(RequestLog.id).desc())
            .limit(limit)
        )
    ).all()

    endpoint_rows = (
        await db.execute(
            select(
                RequestLog.endpoint.label("endpoint"),
                func.count(RequestLog.id).label("requests"),
                func.coalesce(func.avg(RequestLog.duration_ms), 0).label("avg_latency_ms"),
                func.coalesce(func.max(RequestLog.duration_ms), 0).label("max_latency_ms"),
            )
            .where(*filtered)
            .group_by(RequestLog.endpoint)
            .order_by(func.count(RequestLog.id).desc())
            .limit(limit)
        )
    ).all()

    recent_rows = (
        await db.execute(
            select(RequestLog, User)
            .join(User, RequestLog.user_id == User.id)
            .where(*filtered)
            .order_by(RequestLog.created_at.desc())
            .limit(limit)
        )
    ).all()
    slow_rows = (
        await db.execute(
            select(RequestLog, User)
            .join(User, RequestLog.user_id == User.id)
            .where(*filtered)
            .order_by(RequestLog.duration_ms.desc())
            .limit(limit)
        )
    ).all()

    hourly_rows = (
        await db.execute(
            select(
                func.date_format(RequestLog.created_at, "%Y-%m-%d %H:00:00").label("hour"),
                func.count(RequestLog.id).label("requests"),
                func.coalesce(func.avg(RequestLog.duration_ms), 0).label("avg_latency_ms"),
                func.coalesce(func.max(RequestLog.duration_ms), 0).label("max_latency_ms"),
            )
            .where(*filtered)
            .group_by(func.date_format(RequestLog.created_at, "%Y-%m-%d %H:00:00"))
            .order_by(func.date_format(RequestLog.created_at, "%Y-%m-%d %H:00:00").desc())
            .limit(24)
        )
    ).all()

    def serialize_log(row) -> dict:
        log, user = row
        return {
            "created_at": log.created_at,
            "user_id": log.user_id,
            "user": _display_name(user.username, getattr(user, "email", None), user.external_id, user.id),
            "endpoint": log.endpoint,
            "model": getattr(log, "customer_model_alias", "") or log.model,
            "provider_model": getattr(log, "provider_model", "") or log.model,
            "billable_sku": getattr(log, "billable_sku", "") or log.model,
            "duration_ms": log.duration_ms,
            "status_code": log.status_code,
            "route_reason": getattr(log, "route_reason", ""),
            "channel_id": getattr(log, "channel_id", ""),
            "channel_type": getattr(log, "channel_type", ""),
            "provider_platform": getattr(log, "provider_platform", ""),
            "provider_account_fingerprint": getattr(log, "provider_account_fingerprint", ""),
            "fallback_from_channel_id": getattr(log, "fallback_from_channel_id", ""),
            "route_attempt": getattr(log, "route_attempt", 0),
            "input_tokens": log.input_tokens,
            "output_tokens": log.output_tokens,
            "cost_cents": log.cost_cents,
            "upstream_request_id": getattr(log, "upstream_request_id", ""),
        }

    return {
        "period": period,
        "days": days,
        **_analytics_period_fields(period, days, start_day, since),
        "model": model,
        "summary": {
            "requests": total,
            "failed_requests": failed,
            "failure_rate": _safe_rate(failed, total),
            "fallback_requests": int(_row_value(summary, "fallback_requests", 0) or 0),
            "fallback_rate": _safe_rate(int(_row_value(summary, "fallback_requests", 0) or 0), total),
            "min_latency_ms": int(float(_row_value(summary, "min_latency_ms", 0) or 0)),
            "avg_latency_ms": int(float(_row_value(summary, "avg_latency_ms", 0) or 0)),
            "p50_latency_ms": percentile(latency_points, 0.50),
            "p90_latency_ms": percentile(latency_points, 0.90),
            "p95_latency_ms": percentile(latency_points, 0.95),
            "max_latency_ms": int(float(_row_value(summary, "max_latency_ms", 0) or 0)),
            "tokens": int(_row_value(summary, "tokens", 0) or 0),
            "user_charge_cents": int(_row_value(summary, "user_charge_cents", 0) or 0),
            "slow_counts": slow_counts,
        },
        "by_user": [
            {
                "user_id": _row_value(row, "user_id", ""),
                "display_name": _display_name(
                    _row_value(row, "username", None),
                    _row_value(row, "email", None),
                    _row_value(row, "external_id", None),
                    _row_value(row, "user_id", ""),
                ),
                "requests": int(_row_value(row, "requests", 0) or 0),
                "avg_latency_ms": int(float(_row_value(row, "avg_latency_ms", 0) or 0)),
                "max_latency_ms": int(float(_row_value(row, "max_latency_ms", 0) or 0)),
                "user_charge_cents": int(_row_value(row, "user_charge_cents", 0) or 0),
            }
            for row in user_rows
        ],
        "by_route": [
            {
                "route_reason": _row_value(row, "route_reason", "") or "-",
                "requests": int(_row_value(row, "requests", 0) or 0),
                "avg_latency_ms": int(float(_row_value(row, "avg_latency_ms", 0) or 0)),
                "max_latency_ms": int(float(_row_value(row, "max_latency_ms", 0) or 0)),
                "failed_requests": int(_row_value(row, "failed_requests", 0) or 0),
                "fallback_requests": int(_row_value(row, "fallback_requests", 0) or 0),
            }
            for row in route_rows
        ],
        "by_endpoint": [
            {
                "endpoint": _row_value(row, "endpoint", "") or "-",
                "requests": int(_row_value(row, "requests", 0) or 0),
                "avg_latency_ms": int(float(_row_value(row, "avg_latency_ms", 0) or 0)),
                "max_latency_ms": int(float(_row_value(row, "max_latency_ms", 0) or 0)),
            }
            for row in endpoint_rows
        ],
        "hourly": [
            {
                "hour": _row_value(row, "hour", ""),
                "requests": int(_row_value(row, "requests", 0) or 0),
                "avg_latency_ms": int(float(_row_value(row, "avg_latency_ms", 0) or 0)),
                "max_latency_ms": int(float(_row_value(row, "max_latency_ms", 0) or 0)),
            }
            for row in hourly_rows
        ],
        "slow_requests": [serialize_log(row) for row in slow_rows],
        "recent_requests": [serialize_log(row) for row in recent_rows],
    }


@router.get("/analytics/channel-health", dependencies=[Depends(admin_guard)])
async def analytics_channel_health(period: str = "today", db: AsyncSession = Depends(get_db)):
    period, days, _start_day, since = _analytics_period(period)
    request_charge = _request_user_charge_expr()
    upstream_cost = _request_upstream_cost_expr()
    channel_type = _request_channel_type_expr()
    fallback_expr = case((func.lower(func.coalesce(RequestLog.route_reason, "")).like("%fallback%"), 1), else_=0)
    rows = (
        await db.execute(
            select(
                channel_type.label("channel_type"),
                func.count(RequestLog.id).label("requests"),
                func.coalesce(func.sum(case((RequestLog.status_code < 400, 1), else_=0)), 0).label("success"),
                func.coalesce(func.sum(case((RequestLog.status_code >= 400, 1), else_=0)), 0).label("failed"),
                func.coalesce(func.sum(request_charge), 0).label("user_charge_cents"),
                func.coalesce(func.sum(upstream_cost), 0).label("upstream_cost_cents"),
                func.coalesce(func.sum(fallback_expr), 0).label("fallback_count"),
                func.coalesce(func.avg(RequestLog.duration_ms), 0).label("avg_latency_ms"),
            )
            .where(RequestLog.created_at >= since)
            .group_by(channel_type)
            .order_by(func.count(RequestLog.id).desc())
        )
    ).all()
    channel_rows = (
        await db.execute(
            select(
                RequestLog.channel_id.label("channel_id"),
                RequestLog.provider_platform.label("provider_platform"),
                RequestLog.provider_account_fingerprint.label("provider_account_fingerprint"),
                channel_type.label("channel_type"),
                func.count(RequestLog.id).label("requests"),
                func.coalesce(func.sum(case((RequestLog.status_code < 400, 1), else_=0)), 0).label("success"),
                func.coalesce(func.sum(case((RequestLog.status_code >= 400, 1), else_=0)), 0).label("failed"),
                func.coalesce(func.sum(fallback_expr), 0).label("fallback_count"),
                func.coalesce(func.avg(RequestLog.duration_ms), 0).label("avg_latency_ms"),
            )
            .where(RequestLog.created_at >= since, RequestLog.channel_id != "")
            .group_by(RequestLog.channel_id, RequestLog.provider_platform, RequestLog.provider_account_fingerprint, channel_type)
            .order_by(func.count(RequestLog.id).desc())
            .limit(100)
        )
    ).all()
    total_requests = sum(int(_row_value(row, "requests", 0) or 0) for row in rows)
    data = []
    for row in rows:
        requests = int(_row_value(row, "requests", 0) or 0)
        failed = int(_row_value(row, "failed", 0) or 0)
        charge = int(_row_value(row, "user_charge_cents", 0) or 0)
        cost = int(_row_value(row, "upstream_cost_cents", 0) or 0)
        data.append({
            "channel_type": _row_value(row, "channel_type", "unknown") or "unknown",
            "requests": requests,
            "request_share": _safe_rate(requests, total_requests),
            "success": int(_row_value(row, "success", 0) or 0),
            "failed": failed,
            "user_charge_cents": charge,
            "upstream_cost_cents": cost,
            "gross_margin_cents": charge - cost,
            "gross_margin_rate": _safe_rate(charge - cost, charge),
            "fallback_count": int(_row_value(row, "fallback_count", 0) or 0),
            "avg_latency_ms": int(float(_row_value(row, "avg_latency_ms", 0) or 0)),
            "p95_latency_ms": None,
            "failure_rate": _safe_rate(failed, requests),
        })
    account_pool = next((item for item in data if item["channel_type"] == "account_pool"), None)
    known_requests = sum(item["requests"] for item in data if item["channel_type"] != "unknown")
    per_channel = []
    for row in channel_rows:
        requests = int(_row_value(row, "requests", 0) or 0)
        failed = int(_row_value(row, "failed", 0) or 0)
        per_channel.append({
            "channel_id": _row_value(row, "channel_id", "") or "",
            "provider_platform": _row_value(row, "provider_platform", "") or "",
            "provider_account_fingerprint": _row_value(row, "provider_account_fingerprint", "") or "",
            "channel_type": _row_value(row, "channel_type", "unknown") or "unknown",
            "requests": requests,
            "success": int(_row_value(row, "success", 0) or 0),
            "failed": failed,
            "fallback_count": int(_row_value(row, "fallback_count", 0) or 0),
            "avg_latency_ms": int(float(_row_value(row, "avg_latency_ms", 0) or 0)),
            "failure_rate": _safe_rate(failed, requests),
        })
    user_charge_cents = sum(item["user_charge_cents"] for item in data)
    upstream_cost_cents = sum(item["upstream_cost_cents"] for item in data)
    return {
        "period": period,
        "days": days,
        "total_requests": total_requests,
        "account_pool": account_pool,
        "data": data,
        "per_channel": per_channel,
        "source_quality": _source_quality(
            upstream_cost_cents=upstream_cost_cents,
            user_charge_cents=user_charge_cents,
            channel_known_rate=_safe_rate(known_requests, total_requests),
        ),
        "field_notes": {
            "channel_type": "优先使用 request_logs.channel_type；历史日志缺字段时才回退 route_reason 推断。",
            "provider_account_fingerprint": "新通道可写 provider_account_fingerprint，用于看单账号负载和异常。",
        },
    }


@router.get("/analytics/action-items", dependencies=[Depends(admin_guard)])
async def analytics_action_items(
    period: str = "today",
    limit: int = 12,
    db: AsyncSession = Depends(get_db),
):
    period, days, _start_day, since = _analytics_period(period)
    limit = max(1, min(limit, 50))
    low_balance = await analytics_low_balance_users(period="7d", limit=5, db=db)
    errors = await analytics_errors(period=period, limit=5, db=db)
    channel = await analytics_channel_health(period=period, db=db)
    revenue = await analytics_revenue_margin(period=period, db=db)
    usage = await analytics_usage_structure(period=period, limit=5, db=db)
    return _build_action_items(
        period=period,
        days=days,
        low_balance=low_balance,
        errors=errors,
        channel=channel,
        revenue=revenue,
        usage=usage,
        limit=limit,
    )


@router.get("/analytics/operating-dashboard", dependencies=[Depends(admin_guard)])
async def analytics_operating_dashboard(period: str = "today", db: AsyncSession = Depends(get_db)):
    period, days, start_day, _since = _analytics_period(period)
    end_at = datetime.utcnow()
    cache_key = f"operating-dashboard:{period}"
    now_ts = time.time()
    cached = _analytics_dashboard_cache.get(cache_key)
    if cached and now_ts - cached[0] < ANALYTICS_DASHBOARD_CACHE_TTL_SECONDS:
        cached_payload = dict(cached[1])
        cached_payload["cache"] = _analytics_meta(generated_at=cached[1]["generated_at"], cache_hit=True)
        return cached_payload
    overview = await analytics_overview(period=period, db=db)
    growth = await analytics_growth(period=period, db=db)
    revenue = await analytics_revenue_margin(period=period, db=db)
    usage = await analytics_usage_structure(period=period, limit=10, db=db)
    channel = await analytics_channel_health(period=period, db=db)
    errors = await analytics_errors(period=period, limit=8, db=db)
    low_balance = await analytics_low_balance_users(period="7d", limit=8, db=db)
    actions = _build_action_items(
        period=period,
        days=days,
        low_balance=low_balance,
        errors=errors,
        channel=channel,
        revenue=revenue,
        usage=usage,
        limit=8,
    )

    revenue_judgement = (
        "毛利可计算，关注毛利率变化"
        if (revenue.get("source_quality") or {}).get("upstream_cost_available")
        else "缺上游真实成本，无法判断毛利"
    )
    account_pool = channel.get("account_pool") or {}
    account_pool_share = _safe_rate(account_pool.get("requests", 0), channel.get("total_requests", 0))
    channel_judgement = (
        f"号池占比 {account_pool_share * 100:.1f}%，但缺真实成本，近24小时无法判断是否赚钱"
        if account_pool
        else "缺 channel_type，无法管理号池"
    )
    growth_judgement = (
        "近24小时没有新增接入，消耗来自存量用户，需看拉新/转化"
        if int(growth.get("new_users") or 0) == 0 and int(growth.get("first_call_users") or 0) > 0
        else f"新增注册 {growth.get('new_users', 0)}，创建 Key {growth.get('new_api_key_users', 0)}，首次调用 {growth.get('first_call_users', 0)}，首充 {growth.get('first_paid_users', 0)}。"
    )
    high_actions = [item for item in (actions.get("items") or []) if item.get("severity") == "high"]
    judgement = {
        "overall": (
            f"{'有高优先级动作' if high_actions else '暂无高优先级异常'}；"
            f"近24小时新增 {growth.get('new_users', 0)}，首充 {growth.get('first_paid_users', 0)}，"
            f"消耗 {revenue.get('user_charge_cents', 0) / 100:.2f} 美元。"
        ),
        "growth": growth_judgement,
        "revenue": revenue_judgement,
        "channel": channel_judgement,
        "risk": f"近24小时动作 {len(actions.get('items') or [])} 条，其中高优先级 {len(high_actions)} 条。",
    }
    payload = {
        "period": period,
        "days": days,
        **_analytics_period_fields(period, days, start_day, _since, end_at=end_at),
        "generated_at": datetime.utcnow(),
        "judgement": judgement,
        "overview": overview,
        "growth": growth,
        "revenue_margin": revenue,
        "usage_structure": usage,
        "channel_health": channel,
        "action_items": actions,
        "errors": errors,
        "low_balance": low_balance,
    }
    payload["cache"] = _analytics_meta(generated_at=payload["generated_at"], cache_hit=False)
    _analytics_dashboard_cache[cache_key] = (now_ts, payload)
    return payload


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
    billing_by_user = await _admin_billing_states_batch(db, users)
    rows = []
    for user in users:
        billing = billing_by_user.get(user.id, {})
        finance = _admin_finance_summary(snapshots.get(user.id, {}), billing)
        rows.append({
            "user_id": user.id,
            "username": user.username,
            "email": getattr(user, "email", None),
            "email_verified_at": getattr(user, "email_verified_at", None),
            "external_id": user.external_id,
            "created_at": user.created_at,
            "status": user.status,
            **_admin_available_fields(billing),
            "billing": billing,
            "billing_summary": _billing_summary_for_admin(billing),
            "finance_summary": finance,
        })
    return rows


@router.get("/finance/overview", dependencies=[Depends(admin_guard)])
async def finance_overview(db: AsyncSession = Depends(get_db)):
    now = datetime.utcnow()
    today_start = datetime.combine(date.today(), datetime.min.time())
    month_start = datetime.combine(date.today().replace(day=1), datetime.min.time())

    confirmed_orders = (
        await db.execute(
            select(PaymentOrder).where(
                PaymentOrder.status == "confirmed",
                PaymentOrder.confirmed_at >= month_start,
            )
        )
    ).scalars().all()
    today_orders = [
        order for order in confirmed_orders
        if getattr(order, "confirmed_at", None) and order.confirmed_at >= today_start
    ]

    total_paid_balance_cents = await db.scalar(
        select(func.coalesce(func.sum(UserFinanceSummary.total_paid_balance_cents), 0))
    ) or 0
    total_paid_rmb_cents = await db.scalar(
        select(func.coalesce(func.sum(UserFinanceSummary.total_paid_rmb_cents), 0))
    ) or 0
    total_ops_credit_cents = await db.scalar(
        select(func.coalesce(func.sum(UserFinanceSummary.total_ops_credit_cents), 0))
    ) or 0
    total_bonus_cents = await db.scalar(
        select(func.coalesce(func.sum(UserFinanceSummary.total_bonus_cents), 0))
    ) or 0
    total_consumed_cents = await db.scalar(
        select(func.coalesce(func.sum(UserFinanceSummary.total_consumed_cents), 0))
    ) or 0
    total_ops_debit_cents = await db.scalar(
        select(func.coalesce(func.sum(UserFinanceSummary.total_ops_debit_cents), 0))
    ) or 0

    legacy_balance_cents = await db.scalar(select(func.coalesce(func.sum(User.balance), 0))) or 0
    projected_subscription_remaining = case(
        (
            UserSubscription.period_start.is_(None)
            | UserSubscription.period_end.is_(None)
            | (UserSubscription.period_end <= now),
            UserSubscription.quota_cents,
        ),
        else_=func.greatest(
            UserSubscription.quota_cents - UserSubscription.used_cents,
            0,
        ),
    )
    subscription_remaining_cents = await db.scalar(
        select(func.coalesce(func.sum(projected_subscription_remaining), 0)).where(
            UserSubscription.paid_until > now,
        )
    ) or 0
    traffic_pack_remaining_cents = await db.scalar(
        select(func.coalesce(func.sum(TrafficPackBalance.remaining_cents), 0)).where(
            TrafficPackBalance.status == "active",
            TrafficPackBalance.expires_at > now,
            TrafficPackBalance.remaining_cents > 0,
        )
    ) or 0
    credit_wallet_cents = await db.scalar(
        select(func.coalesce(func.sum(CreditBalance.remaining_cents), 0)).where(
            CreditBalance.status == "active",
            CreditBalance.remaining_cents > 0,
        )
    ) or 0
    available_cents = (
        int(legacy_balance_cents)
        + int(subscription_remaining_cents)
        + int(traffic_pack_remaining_cents)
        + int(credit_wallet_cents)
    )

    return {
        "generated_at": now,
        "today": {
            "paid_order_count": len(today_orders),
            "paid_user_count": len({order.user_id for order in today_orders}),
            "paid_rmb_cents": sum(_money_text_to_minor_cents(order.amount_rmb) for order in today_orders),
            "paid_balance_cents": sum(int(order.add_balance_cents or 0) for order in today_orders),
        },
        "month": {
            "paid_order_count": len(confirmed_orders),
            "paid_user_count": len({order.user_id for order in confirmed_orders}),
            "paid_rmb_cents": sum(_money_text_to_minor_cents(order.amount_rmb) for order in confirmed_orders),
            "paid_balance_cents": sum(int(order.add_balance_cents or 0) for order in confirmed_orders),
        },
        "totals": {
            "paid_rmb_cents": int(total_paid_rmb_cents),
            "paid_balance_cents": int(total_paid_balance_cents),
            "ops_credit_cents": int(total_ops_credit_cents),
            "ops_debit_cents": int(total_ops_debit_cents),
            "bonus_cents": int(total_bonus_cents),
            "consumed_cents": int(total_consumed_cents),
            "available_cents": int(available_cents),
            "available_usd": int(available_cents) / 100,
            "credit_wallet_cents": int(credit_wallet_cents),
            "credit_wallet_usd": int(credit_wallet_cents) / 100,
            "legacy_balance_cents": int(legacy_balance_cents),
            "subscription_remaining_cents": int(subscription_remaining_cents),
            "traffic_pack_remaining_cents": int(traffic_pack_remaining_cents),
        },
    }


def _finance_user_payload(user: Optional[User]) -> dict:
    if not user:
        return {
            "user_id": "",
            "username": "",
            "email": "",
            "external_id": "",
            "display_name": "",
        }
    return {
        "user_id": user.id,
        "username": user.username,
        "email": getattr(user, "email", None),
        "external_id": user.external_id,
        "display_name": _display_name(user.username, getattr(user, "email", None), user.external_id, user.id),
    }


@router.get("/finance/ledger", dependencies=[Depends(admin_guard)])
async def finance_ledger(
    search: Optional[str] = None,
    type_filter: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    limit = max(1, min(limit, 300))
    normalized_type = (type_filter or "").strip().lower()
    search_text = (search or "").strip()
    start_at = _parse_admin_date(date_from)
    end_at = _parse_admin_date(date_to, end_of_day=True)

    def in_window(value: Optional[datetime]) -> bool:
        if value is None:
            return False
        value = _normalize_utc_naive(value)
        if start_at and value < start_at:
            return False
        if end_at and value > end_at:
            return False
        return True

    events: list[dict] = []

    if normalized_type in {"", "payment", "credit"}:
        query = select(PaymentOrder, User).join(User, PaymentOrder.user_id == User.id).where(
            PaymentOrder.status == "confirmed",
            PaymentOrder.confirmed_at.is_not(None),
        )
        if start_at:
            query = query.where(PaymentOrder.confirmed_at >= start_at)
        if end_at:
            query = query.where(PaymentOrder.confirmed_at <= end_at)
        if search_text:
            pat = f"%{search_text}%"
            query = query.where(
                User.username.ilike(pat)
                | User.email.ilike(pat)
                | User.external_id.ilike(pat)
                | User.id.ilike(pat)
                | PaymentOrder.order_no.ilike(pat)
                | PaymentOrder.trade_no.ilike(pat)
            )
        rows = (
            await db.execute(query.order_by(PaymentOrder.confirmed_at.desc()).limit(limit))
        ).all()
        for order, user in rows:
            events.append({
                "event_id": f"payment:{order.order_no}",
                "event_type": "payment_confirmed",
                "event_group": "payment",
                "created_at": order.confirmed_at,
                "amount_cents": int(order.add_balance_cents or 0),
                "cash_rmb_cents": _money_text_to_minor_cents(order.amount_rmb),
                "balance_after_cents": None,
                "source_type": "payment_order",
                "source_id": order.order_no,
                "product_id": order.product_id,
                "product": _product_admin_payload(order.product_id),
                "note": f"trade_no: {order.trade_no}" if order.trade_no else "",
                "user": _finance_user_payload(user),
            })

    if normalized_type in {"", "recharge", "credit"}:
        query = select(RechargeLog, User).join(User, RechargeLog.user_id == User.id)
        if start_at:
            query = query.where(RechargeLog.created_at >= start_at)
        if end_at:
            query = query.where(RechargeLog.created_at <= end_at)
        if search_text:
            pat = f"%{search_text}%"
            query = query.where(
                User.username.ilike(pat)
                | User.email.ilike(pat)
                | User.external_id.ilike(pat)
                | User.id.ilike(pat)
                | RechargeLog.order_id.ilike(pat)
                | RechargeLog.note.ilike(pat)
            )
        rows = (
            await db.execute(query.order_by(RechargeLog.created_at.desc()).limit(limit))
        ).all()
        for log, user in rows:
            event_type = "webhook_payment_credit" if log.amount and log.balance_added > 0 else "webhook_credit"
            events.append({
                "event_id": f"recharge:{log.id}",
                "event_type": event_type,
                "event_group": "recharge",
                "created_at": log.created_at,
                "amount_cents": int(log.balance_added or 0),
                "cash_rmb_cents": int(log.amount or 0),
                "balance_after_cents": None,
                "source_type": "recharge_log",
                "source_id": log.order_id,
                "product_id": "",
                "product": _product_admin_payload(""),
                "note": log.note or "",
                "user": _finance_user_payload(user),
            })

    if normalized_type in {"", "billing", "admin", "usage", "credit", "debit"}:
        query = select(BillingLedgerEntry, User).join(User, BillingLedgerEntry.user_id == User.id)
        if start_at:
            query = query.where(BillingLedgerEntry.created_at >= start_at)
        if end_at:
            query = query.where(BillingLedgerEntry.created_at <= end_at)
        if normalized_type == "admin":
            query = query.where(BillingLedgerEntry.source_type == "admin")
        elif normalized_type == "usage":
            query = query.where(BillingLedgerEntry.amount_cents < 0)
        elif normalized_type == "credit":
            query = query.where(BillingLedgerEntry.amount_cents > 0)
        elif normalized_type == "debit":
            query = query.where(BillingLedgerEntry.amount_cents < 0)
        if search_text:
            pat = f"%{search_text}%"
            query = query.where(
                User.username.ilike(pat)
                | User.email.ilike(pat)
                | User.external_id.ilike(pat)
                | User.id.ilike(pat)
                | BillingLedgerEntry.source_id.ilike(pat)
                | BillingLedgerEntry.note.ilike(pat)
                | BillingLedgerEntry.entry_type.ilike(pat)
            )
        rows = (
            await db.execute(query.order_by(BillingLedgerEntry.created_at.desc()).limit(limit))
        ).all()
        for entry, user in rows:
            events.append({
                "event_id": f"billing:{entry.id}",
                "event_type": entry.entry_type,
                "event_group": "billing",
                "created_at": entry.created_at,
                "amount_cents": int(entry.amount_cents or 0),
                "cash_rmb_cents": 0,
                "balance_after_cents": int(entry.balance_after_cents or 0),
                "source_type": entry.source_type,
                "source_id": entry.source_id,
                "product_id": entry.product_id,
                "product": _product_admin_payload(entry.product_id),
                "note": entry.note or "",
                "user": _finance_user_payload(user),
            })

    if normalized_type in {"", "redemption", "bonus", "credit"}:
        query = select(RedemptionCodeUse, User).join(User, RedemptionCodeUse.user_id == User.id)
        if start_at:
            query = query.where(RedemptionCodeUse.created_at >= start_at)
        if end_at:
            query = query.where(RedemptionCodeUse.created_at <= end_at)
        if search_text:
            pat = f"%{search_text}%"
            query = query.where(
                User.username.ilike(pat)
                | User.email.ilike(pat)
                | User.external_id.ilike(pat)
                | User.id.ilike(pat)
                | RedemptionCodeUse.code.ilike(pat)
            )
        rows = (
            await db.execute(query.order_by(RedemptionCodeUse.created_at.desc()).limit(limit))
        ).all()
        for use, user in rows:
            events.append({
                "event_id": f"redemption:{use.id}",
                "event_type": "redemption_credit",
                "event_group": "redemption",
                "created_at": use.created_at,
                "amount_cents": int(use.balance_cents or 0),
                "cash_rmb_cents": 0,
                "balance_after_cents": None,
                "source_type": "redemption_code",
                "source_id": use.code,
                "product_id": "",
                "product": _product_admin_payload(""),
                "note": "兑换码入账",
                "user": _finance_user_payload(user),
            })

    if normalized_type in {"", "referral", "bonus", "credit"}:
        query = select(ReferralReward, User).join(
            User,
            func.coalesce(ReferralReward.recipient_id, ReferralReward.referrer_id) == User.id,
        )
        if start_at:
            query = query.where(ReferralReward.created_at >= start_at)
        if end_at:
            query = query.where(ReferralReward.created_at <= end_at)
        if search_text:
            pat = f"%{search_text}%"
            query = query.where(
                User.username.ilike(pat)
                | User.email.ilike(pat)
                | User.external_id.ilike(pat)
                | User.id.ilike(pat)
                | ReferralReward.order_no.ilike(pat)
                | ReferralReward.reward_type.ilike(pat)
            )
        rows = (
            await db.execute(query.order_by(ReferralReward.created_at.desc()).limit(limit))
        ).all()
        for reward, user in rows:
            events.append({
                "event_id": f"referral:{reward.id}",
                "event_type": f"referral_{reward.reward_type or 'reward'}",
                "event_group": "referral",
                "created_at": reward.created_at,
                "amount_cents": int(reward.reward_cents or 0),
                "cash_rmb_cents": 0,
                "balance_after_cents": None,
                "source_type": "referral_reward",
                "source_id": reward.order_no or reward.id,
                "product_id": "",
                "product": _product_admin_payload(""),
                "note": f"reward_type: {reward.reward_type}",
                "user": _finance_user_payload(user),
            })

    events = [event for event in events if in_window(event.get("created_at"))]
    events.sort(key=lambda item: item.get("created_at") or datetime.min, reverse=True)
    return {
        "items": events[:limit],
        "limit": limit,
        "filters": {
            "search": search_text,
            "type_filter": normalized_type,
            "date_from": start_at,
            "date_to": end_at,
        },
        "source_note": "聚合 PaymentOrder、RechargeLog、BillingLedgerEntry、RedemptionCode 和 ReferralReward；逐请求扣费仍在请求日志中查看。",
    }


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
    fallback_requests = (
        await db.execute(
            select(func.count()).select_from(RequestLog).where(RequestLog.created_at >= since, RequestLog.route_attempt > 0)
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
            "fallback_requests": int(fallback_requests),
            "fallback_rate": (float(fallback_requests) / float(total_requests)) if total_requests else 0,
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
                    "channel_id": getattr(log, "channel_id", ""),
                    "channel_type": getattr(log, "channel_type", ""),
                    "provider_platform": getattr(log, "provider_platform", ""),
                    "fallback_from_channel_id": getattr(log, "fallback_from_channel_id", ""),
                    "route_attempt": getattr(log, "route_attempt", 0),
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
                "video_count": getattr(log, "video_count", 0),
                "usage_unit_type": getattr(log, "usage_unit_type", "tokens"),
                "usage_unit_count": getattr(log, "usage_unit_count", 0),
                "billable_sku": getattr(log, "billable_sku", "") or (getattr(log, "customer_model_alias", "") or log.model),
                "upstream_request_id": getattr(log, "upstream_request_id", ""),
                "price_version": getattr(log, "price_version", 0),
                "pricing_mode": getattr(log, "pricing_mode", ""),
                "model_multiplier": getattr(log, "model_multiplier", 1.0),
                "output_multiplier": getattr(log, "output_multiplier", 1.0),
                "cache_read_multiplier": getattr(log, "cache_read_multiplier", 0.0),
                "image_multiplier": getattr(log, "image_multiplier", 1.0),
                "video_multiplier": getattr(log, "video_multiplier", 1.0),
                "base_price_input_per_million": getattr(log, "base_price_input_per_million", 0),
                "base_price_output_per_million": getattr(log, "base_price_output_per_million", 0),
                "effective_cached_input_per_million": getattr(log, "effective_cached_input_per_million", 0.0),
                "total_tokens": log.input_tokens + log.output_tokens,
                "cost_cents": log.cost_cents,
                "cost_usd": log.cost_cents / 100,
                "duration_ms": log.duration_ms,
                "status_code": log.status_code,
                "route_reason": getattr(log, "route_reason", ""),
                "channel_id": getattr(log, "channel_id", ""),
                "channel_type": getattr(log, "channel_type", ""),
                "provider_platform": getattr(log, "provider_platform", ""),
                "provider_account_fingerprint": getattr(log, "provider_account_fingerprint", ""),
                "fallback_from_channel_id": getattr(log, "fallback_from_channel_id", ""),
                "route_attempt": getattr(log, "route_attempt", 0),
            }
            for log in logs
        ],
    }


# ============== Redemption Code Management ==============

def _generate_code() -> str:
    parts = [secrets.token_hex(2).upper() for _ in range(4)]
    return f"CC-{parts[0]}-{parts[1]}-{parts[2]}-{parts[3]}"


def _normalize_redemption_code(value: str) -> str:
    return value.strip()


@router.post("/redemption-codes/generate", dependencies=[Depends(admin_guard)],
             response_model=RedemptionGenerateResponse)
async def generate_redemption_codes(
    payload: RedemptionGenerateRequest, db: AsyncSession = Depends(get_db)
):
    if payload.code and payload.count != 1:
        raise HTTPException(status_code=400, detail="custom code can only be generated once")
    codes = []
    for _ in range(payload.count):
        code_str = _normalize_redemption_code(payload.code) if payload.code else _generate_code()
        existing = (
            await db.execute(select(RedemptionCode).where(RedemptionCode.code == code_str))
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(status_code=409, detail="redemption code already exists")
        code = RedemptionCode(
            id=generate_id("rc_"),
            code=code_str,
            balance_cents=payload.balance_cents,
            status="unused",
            max_redemptions=payload.max_redemptions,
            per_user_limit=payload.per_user_limit,
            redemption_count=0,
            note=(payload.note or "").strip(),
        )
        db.add(code)
        codes.append(code_str)
    await db.commit()
    return RedemptionGenerateResponse(
        codes=codes,
        balance_cents=payload.balance_cents,
        count=payload.count,
        max_redemptions=payload.max_redemptions,
        per_user_limit=payload.per_user_limit,
        note=(payload.note or "").strip(),
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
            "max_redemptions": int(getattr(c, "max_redemptions", 1) or 0),
            "per_user_limit": int(getattr(c, "per_user_limit", 1) or 0),
            "redemption_count": int(getattr(c, "redemption_count", 0) or 0),
            "used_by": c.used_by,
            "used_at": c.used_at,
            "note": getattr(c, "note", "") or "",
            "created_at": c.created_at,
        }
        for c in codes
    ]


@router.patch("/redemption-codes/{code_id}", dependencies=[Depends(admin_guard)])
async def update_redemption_code(
    code_id: str,
    payload: RedemptionCodeUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(RedemptionCode).where(RedemptionCode.id == code_id))
    code = result.scalar_one_or_none()
    if not code:
        raise HTTPException(status_code=404, detail="code not found")
    current_count = int(getattr(code, "redemption_count", 0) or 0)
    if payload.max_redemptions is not None and payload.max_redemptions > 0 and payload.max_redemptions < current_count:
        raise HTTPException(status_code=400, detail="max_redemptions cannot be lower than redemption_count")
    if payload.balance_cents is not None:
        code.balance_cents = payload.balance_cents
    if payload.max_redemptions is not None:
        code.max_redemptions = payload.max_redemptions
    if payload.per_user_limit is not None:
        code.per_user_limit = payload.per_user_limit
    if payload.note is not None:
        code.note = payload.note.strip()
    if payload.status is not None:
        code.status = payload.status
    await db.commit()
    return {
        "id": code.id,
        "code": code.code,
        "balance_cents": code.balance_cents,
        "status": code.status,
        "max_redemptions": int(getattr(code, "max_redemptions", 1) or 0),
        "per_user_limit": int(getattr(code, "per_user_limit", 1) or 0),
        "redemption_count": current_count,
        "note": getattr(code, "note", "") or "",
    }


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
    search: Optional[str] = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(PaymentOrder, User)
        .outerjoin(User, PaymentOrder.user_id == User.id)
        .order_by(PaymentOrder.created_at.desc())
    )
    if status_filter:
        query = query.where(PaymentOrder.status == status_filter)
    if search:
        pat = f"%{search.strip()}%"
        query = query.where(
            PaymentOrder.user_id.ilike(pat)
            | PaymentOrder.order_no.ilike(pat)
            | PaymentOrder.trade_no.ilike(pat)
            | User.username.ilike(pat)
            | User.email.ilike(pat)
            | User.external_id.ilike(pat)
        )
    result = await db.execute(query.limit(limit))
    rows = result.all()
    items = []
    for row in rows:
        try:
            o = row[0]
            user = row[1] if len(row) > 1 else None
            if not hasattr(o, "order_no"):
                raise TypeError("unexpected payment order row")
        except (TypeError, IndexError, KeyError):
            o = row
            user = None
        username = getattr(user, "username", None) if user else None
        email = getattr(user, "email", None) if user else None
        external_id = getattr(user, "external_id", None) if user else None
        items.append({
            **_product_admin_payload(getattr(o, "product_id", "") or ""),
            "id": o.id,
            "user_id": o.user_id,
            "username": username,
            "email": email,
            "external_id": external_id,
            "display_name": _display_name(username, email, external_id, o.user_id),
            "order_no": o.order_no,
            "amount_rmb": o.amount_rmb,
            "add_balance_cents": o.add_balance_cents,
            "catalog_version": getattr(o, "catalog_version", None),
            "purchase_action": getattr(o, "purchase_action", None),
            "promised_credit_cents": getattr(o, "promised_credit_cents", None),
            "status": o.status,
            "trade_no": o.trade_no,
            "pay_url": o.pay_url,
            "created_at": o.created_at,
            "confirmed_at": o.confirmed_at,
        })
    return items


@router.post("/payment-orders/{order_no}/force-confirm", dependencies=[Depends(admin_guard)])
async def force_confirm_order(order_no: str, db: AsyncSession = Depends(get_db)):
    """Admin 手动补单：查询支付服务验证后强制入账。"""
    from .payment import _attach_available_balance, _confirm_with_query_fallback

    order = (
        await db.execute(select(PaymentOrder).where(PaymentOrder.order_no == order_no))
    ).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="order not found")
    if order.status == "confirmed":
        try:
            result = await confirm_paid_order(
                order_no=order.order_no,
                money=order.amount_rmb,
                trade_no=order.trade_no or "",
                db=db,
            )
        except PaymentConfirmError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        result = await _attach_available_balance(result, db)
        available_cents = int(result["available_cents"])
        return {
            "order_no": order_no,
            "status": "already_confirmed",
            "trade_no": result["order"].trade_no,
            "added_cents": result["added_cents"],
            "billing_action": result.get("billing_action"),
            "new_balance": available_cents,
            "new_balance_usd": available_cents / 100,
        }

    try:
        result = await _confirm_with_query_fallback(order_no, db)
    except HTTPException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    result = await _attach_available_balance(result, db)
    available_cents = int(result["available_cents"])
    return {
        "order_no": order_no,
        "status": "already_confirmed" if result.get("already_confirmed") else "confirmed",
        "trade_no": result["order"].trade_no,
        "added_cents": result["added_cents"],
        "billing_action": result.get("billing_action"),
        "new_balance": available_cents,
        "new_balance_usd": available_cents / 100,
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
    from .payment import _attach_available_balance

    order = (
        await db.execute(
            select(PaymentOrder)
            .where(PaymentOrder.order_no == order_no)
        )
    ).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="order not found")
    if order.status == "confirmed":
        try:
            result = await confirm_paid_order(
                order_no=order.order_no,
                money=order.amount_rmb,
                trade_no=order.trade_no or "",
                db=db,
            )
        except PaymentConfirmError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        result = await _attach_available_balance(result, db)
        available_cents = int(result["available_cents"])
        return {
            "order_no": order_no,
            "status": "already_confirmed",
            "trade_no": result["order"].trade_no,
            "added_cents": result["added_cents"],
            "billing_action": result.get("billing_action"),
            "new_balance": available_cents,
            "new_balance_usd": available_cents / 100,
        }

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

    result = await _attach_available_balance(result, db)
    available_cents = int(result["available_cents"])

    return {
        "order_no": order_no,
        "status": "already_confirmed" if result.get("already_confirmed") else "confirmed",
        "trade_no": callback["trade_no"],
        "added_cents": result["added_cents"],
        "billing_action": result.get("billing_action"),
        "new_balance": available_cents,
        "new_balance_usd": available_cents / 100,
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
