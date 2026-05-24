import os
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import case, func, select
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
    BillingLedgerEntry,
    ModelAliasOverride,
    ModelPricingOverride,
    SystemSetting,
    PaymentOrder,
    RechargeLog,
    RedemptionCode,
    ReferralReward,
    RequestLog,
    Station,
    StationCustomerLink,
    TrafficPackBalance,
    UsageDaily,
    User,
    Account,
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
    AdminUserPasswordResetResponse, AdminUserUpdate,
    AdminClaudeCompatSettingsUpdate, AdminModelAliasUpdate, AdminModelPricingUpdate, AnnouncementCreate, AnnouncementUpdate,
    RedemptionGenerateRequest, RedemptionGenerateResponse,
)
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
from .router import (
    CLAUDE_COMPAT_PROVIDER_KIRO_GO,
    CLAUDE_COMPAT_PROVIDER_UPSTREAM_DIRECT,
    CLAUDE_COMPAT_PROVIDERS,
)
from .security import decrypt_api_key, encrypt_api_key, generate_api_key, generate_id, hash_key, hash_password, require_admin


router = APIRouter(prefix="/admin", tags=["admin"])
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
    )


def _billing_summary_for_admin(billing: dict) -> dict:
    subscription = billing.get("subscription") or {}
    traffic_packs = billing.get("traffic_packs") or {}
    legacy_balance = billing.get("legacy_balance") or {}
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
        "legacy_balance_cents": int(legacy_balance.get("remaining_cents") or 0),
    }


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
        "owned_by": model.owned_by,
        "provider_name": model.provider_name,
        "provider_model": model.provider_model,
        "delivery_lane": model.delivery_lane,
        "capabilities": list(model.capabilities),
        "billable_sku": model.billable_sku,
        "base_price_input_per_million": model.base_price_input_per_million,
        "base_price_output_per_million": model.base_price_output_per_million,
        "base_price_per_image_cents": model.base_price_per_image_cents,
        "price_input_per_million": model.price_input_per_million,
        "price_output_per_million": model.price_output_per_million,
        "price_per_image_cents": model.price_per_image_cents,
        "effective_cached_input_per_million": model.effective_cached_input_per_million,
        "pricing_mode": model.pricing_mode,
        "model_multiplier": model.model_multiplier,
        "output_multiplier": model.output_multiplier,
        "cache_read_multiplier": model.cache_read_multiplier,
        "image_multiplier": model.image_multiplier,
        "price_version": model.price_version,
        "override_active": model.public_id in model_registry.pricing_overrides,
    }


def admin_guard(request: Request):
    require_admin(request)


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
    return case(
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
        "channel_type_confidence": "route_reason_inferred",
        "channel_known_rate": channel_known_rate,
        "missing_fields": [
            field
            for field, missing in {
                "channel_type": channel_known_rate < 1,
                "provider_pool_id": True,
                "provider_account_fingerprint": True,
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
            "suggested_action": "在请求日志补 channel_type、provider_pool_id、provider_account_fingerprint，并做按通道聚合。",
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
    items = []
    for u, link, station in rows:
        billing = await _admin_billing_state(db, u)
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
            "billing": billing,
            "billing_summary": _billing_summary_for_admin(billing),
            "station_attribution": None if not station else {
                "station_id": station.id,
                "station_name": station.display_name,
                "station_owner_user_id": station.owner_user_id,
                "link_status": getattr(link, "status", None),
            },
        })
    return items


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
    sub.used_cents = min(int(payload.used_cents or 0), int(sub.quota_cents or 0)) if "used_cents" in payload.model_fields_set else min(int(sub.used_cents or 0), int(sub.quota_cents or 0))
    if sub.status == "active":
        normalize_subscription_period(sub, now)

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
        "billing": billing,
        "billing_summary": _billing_summary_for_admin(billing),
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
    user_charge_cents = sum(item["user_charge_cents"] for item in data)
    upstream_cost_cents = sum(item["upstream_cost_cents"] for item in data)
    return {
        "period": period,
        "days": days,
        "total_requests": total_requests,
        "account_pool": account_pool,
        "data": data,
        "source_quality": _source_quality(
            upstream_cost_cents=upstream_cost_cents,
            user_charge_cents=user_charge_cents,
            channel_known_rate=_safe_rate(known_requests, total_requests),
        ),
        "field_notes": {
            "channel_type": "当前由 route_reason 推断，需补稳定枚举 account_pool / official_provider。",
            "provider_account_fingerprint": "未落库，暂不能看单账号负载和异常。",
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
    rows = []
    for user in users:
        billing = await _admin_billing_state(db, user)
        rows.append({
            "user_id": user.id,
            "username": user.username,
            "email": getattr(user, "email", None),
            "email_verified_at": getattr(user, "email_verified_at", None),
            "external_id": user.external_id,
            "created_at": user.created_at,
            "status": user.status,
            "billing": billing,
            "billing_summary": _billing_summary_for_admin(billing),
            "finance_summary": snapshots.get(user.id, {}),
        })
    return rows


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
            **_product_admin_payload(getattr(o, "product_id", "") or ""),
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
        "billing_action": result.get("billing_action"),
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
        "billing_action": result.get("billing_action"),
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
