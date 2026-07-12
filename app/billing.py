from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .credit_wallet import debit_credit_batches, list_spendable_credit_batches
from .models import BillingLedgerEntry, TrafficPackBalance, User, UserSubscription
from .security import generate_id


BILLING_PERIOD_DAYS = 30
TRAFFIC_PACK_VALID_DAYS = 180


class BillingError(Exception):
    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class PaymentProduct:
    id: str
    kind: str
    name: str
    money: str
    balance_cents: int
    rank: int = 0
    min_plan_rank: int = 0

    @property
    def money_decimal(self) -> Decimal:
        return Decimal(self.money).quantize(Decimal("0.01"))

    @property
    def amount_fen(self) -> int:
        return int((self.money_decimal * 100).to_integral_value())

    @property
    def promised_credit_cents(self) -> int:
        return self.balance_cents


CREDIT_CATALOG_VERSION = "credit-v1"
CREDIT_PURCHASE_ACTION = "credit_purchase"

CREDIT_PRODUCTS: tuple[PaymentProduct, ...] = (
    PaymentProduct("credit_light", "credit", "轻量美金额度 $100", "59.90", 10000),
    PaymentProduct("credit_standard", "credit", "标准美金额度 $400", "199.00", 40000),
    PaymentProduct("credit_pro", "credit", "专业美金额度 $1,000", "399.00", 100000),
)

LEGACY_PAYMENT_PRODUCTS: tuple[PaymentProduct, ...] = (
    PaymentProduct("monthly_light", "monthly", "轻量月卡", "49.90", 8000, rank=1),
    PaymentProduct("monthly_basic", "monthly", "基础月卡", "199.00", 40000, rank=2),
    PaymentProduct("monthly_flagship", "monthly", "旗舰月卡", "399.00", 100000, rank=3),
    PaymentProduct("addon_boost", "addon", "补量包", "149.00", 30000, min_plan_rank=1),
    PaymentProduct("addon_project", "addon", "项目包", "399.00", 100000, min_plan_rank=2),
    PaymentProduct("addon_ultra", "addon", "超大包", "699.00", 200000, min_plan_rank=3),
)

PAYMENT_PRODUCTS: tuple[PaymentProduct, ...] = CREDIT_PRODUCTS
HISTORICAL_PAYMENT_PRODUCTS: tuple[PaymentProduct, ...] = CREDIT_PRODUCTS + LEGACY_PAYMENT_PRODUCTS
PRODUCTS_BY_ID: dict[str, PaymentProduct] = {product.id: product for product in HISTORICAL_PAYMENT_PRODUCTS}
CREDIT_PRODUCTS_BY_ID: dict[str, PaymentProduct] = {product.id: product for product in CREDIT_PRODUCTS}
CREDIT_CATALOGS: dict[str, dict[str, PaymentProduct]] = {
    CREDIT_CATALOG_VERSION: CREDIT_PRODUCTS_BY_ID,
}
MONTHLY_PRODUCTS: tuple[PaymentProduct, ...] = tuple(p for p in LEGACY_PAYMENT_PRODUCTS if p.kind == "monthly")
ADDON_PRODUCTS: tuple[PaymentProduct, ...] = tuple(p for p in LEGACY_PAYMENT_PRODUCTS if p.kind == "addon")
MONTHLY_BY_ID: dict[str, PaymentProduct] = {product.id: product for product in MONTHLY_PRODUCTS}
ADDONS_BY_ID: dict[str, PaymentProduct] = {product.id: product for product in ADDON_PRODUCTS}


def utcnow() -> datetime:
    return datetime.utcnow()


def cents_to_usd(cents: int) -> float:
    return int(cents or 0) / 100


def normalize_money(money: str) -> str:
    try:
        value = Decimal(str(money))
        normalized = value.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        raise BillingError("invalid money amount", status_code=400)
    if not value.is_finite() or value != normalized:
        raise BillingError("invalid money amount", status_code=400)
    return format(normalized, "f")


def product_by_id(product_id: Optional[str]) -> Optional[PaymentProduct]:
    return PRODUCTS_BY_ID.get(product_id or "")


def credit_product_by_id(
    product_id: Optional[str],
    catalog_version: str = CREDIT_CATALOG_VERSION,
) -> Optional[PaymentProduct]:
    return CREDIT_CATALOGS.get(catalog_version, {}).get(product_id or "")


def active_subscription(sub: UserSubscription | None, now: datetime | None = None) -> bool:
    if not sub:
        return False
    current = now or utcnow()
    status = getattr(sub, "status", "")
    paid_until = getattr(sub, "paid_until", None)
    return status == "active" and bool(paid_until) and paid_until > current


def _period_end_from(start: datetime, paid_until: datetime) -> datetime:
    return min(start + timedelta(days=BILLING_PERIOD_DAYS), paid_until)


def _ledger(
    *,
    user_id: str,
    entry_type: str,
    amount_cents: int,
    source_type: str = "",
    source_id: str = "",
    product_id: str = "",
    balance_after_cents: int = 0,
    note: str = "",
) -> BillingLedgerEntry:
    return BillingLedgerEntry(
        id=generate_id("bl_"),
        user_id=user_id,
        entry_type=entry_type,
        amount_cents=int(amount_cents or 0),
        source_type=source_type,
        source_id=source_id,
        product_id=product_id,
        balance_after_cents=int(balance_after_cents or 0),
        note=note[:512],
    )


def add_billing_ledger(
    db: AsyncSession,
    *,
    user_id: str,
    entry_type: str,
    amount_cents: int,
    source_type: str = "",
    source_id: str = "",
    product_id: str = "",
    balance_after_cents: int = 0,
    note: str = "",
) -> BillingLedgerEntry:
    entry = _ledger(
        user_id=user_id,
        entry_type=entry_type,
        amount_cents=amount_cents,
        source_type=source_type,
        source_id=source_id,
        product_id=product_id,
        balance_after_cents=balance_after_cents,
        note=note,
    )
    db.add(entry)
    return entry


async def get_subscription_for_update(db: AsyncSession, user_id: str) -> UserSubscription | None:
    return (
        await db.execute(
            select(UserSubscription)
            .where(UserSubscription.user_id == user_id)
            .with_for_update()
        )
    ).scalar_one_or_none()


async def get_subscription(db: AsyncSession, user_id: str) -> UserSubscription | None:
    return (
        await db.execute(select(UserSubscription).where(UserSubscription.user_id == user_id))
    ).scalar_one_or_none()


async def get_traffic_pack_for_update(db: AsyncSession, pack_id: str) -> TrafficPackBalance | None:
    return (
        await db.execute(
            select(TrafficPackBalance)
            .where(TrafficPackBalance.id == pack_id)
            .with_for_update()
        )
    ).scalar_one_or_none()


def _result_rows(result) -> list:
    if result is None:
        return []
    if hasattr(result, "scalars"):
        scalars = result.scalars()
        if hasattr(scalars, "all"):
            return list(scalars.all())
    if hasattr(result, "all"):
        return list(result.all())
    scalar_one_or_none = getattr(result, "scalar_one_or_none", None)
    if callable(scalar_one_or_none):
        value = scalar_one_or_none()
        return [] if value is None else [value]
    scalar = getattr(result, "scalar", None)
    if callable(scalar):
        value = scalar()
        return [] if value is None else [value]
    return []


@dataclass(frozen=True)
class SubscriptionPeriodProjection:
    status: str
    period_start: datetime | None
    period_end: datetime | None
    paid_until: datetime | None
    used_cents: int
    changed: bool


def project_subscription_period(
    sub: UserSubscription | None,
    now: datetime | None = None,
) -> SubscriptionPeriodProjection | None:
    """Project period normalization without mutating the locked ORM row."""
    if not sub:
        return None
    current = now or utcnow()
    original = (
        str(getattr(sub, "status", "") or ""),
        getattr(sub, "period_start", None),
        getattr(sub, "period_end", None),
        int(getattr(sub, "used_cents", 0) or 0),
    )
    paid_until = getattr(sub, "paid_until", None)
    status, period_start, period_end, used_cents = original

    if not paid_until or paid_until <= current:
        status = "expired"
    else:
        status = "active"
        if not period_start or not period_end:
            period_start = current
            period_end = _period_end_from(current, paid_until)
            used_cents = 0
        else:
            while period_end <= current and period_end < paid_until:
                period_start = period_end
                period_end = _period_end_from(period_start, paid_until)
                used_cents = 0

    projected = (status, period_start, period_end, used_cents)
    return SubscriptionPeriodProjection(
        status=status,
        period_start=period_start,
        period_end=period_end,
        paid_until=paid_until,
        used_cents=used_cents,
        changed=projected != original,
    )


def _apply_subscription_period_projection(
    sub: UserSubscription,
    projection: SubscriptionPeriodProjection,
) -> bool:
    if not projection.changed:
        return False
    sub.status = projection.status
    sub.period_start = projection.period_start
    sub.period_end = projection.period_end
    sub.used_cents = projection.used_cents
    return True


def _projected_subscription_available_cents(
    sub: UserSubscription | None,
    projection: SubscriptionPeriodProjection | None,
    now: datetime,
) -> int:
    if (
        not sub
        or not projection
        or projection.status != "active"
        or not projection.paid_until
        or projection.paid_until <= now
    ):
        return 0
    return max(0, int(getattr(sub, "quota_cents", 0) or 0) - projection.used_cents)


def normalize_subscription_period(sub: UserSubscription | None, now: datetime | None = None) -> bool:
    projection = project_subscription_period(sub, now)
    if not sub or not projection:
        return False
    return _apply_subscription_period_projection(sub, projection)


def available_subscription_cents(sub: UserSubscription | None, now: datetime | None = None) -> int:
    if not active_subscription(sub, now):
        return 0
    return max(0, int(getattr(sub, "quota_cents", 0) or 0) - int(getattr(sub, "used_cents", 0) or 0))


async def active_traffic_packs_for_update(db: AsyncSession, user_id: str, now: datetime | None = None) -> list[TrafficPackBalance]:
    current = now or utcnow()
    result = await db.execute(
        select(TrafficPackBalance)
        .where(
            TrafficPackBalance.user_id == user_id,
            TrafficPackBalance.status == "active",
            TrafficPackBalance.remaining_cents > 0,
            TrafficPackBalance.expires_at > current,
        )
        .order_by(
            TrafficPackBalance.expires_at.asc(),
            TrafficPackBalance.created_at.asc(),
            TrafficPackBalance.id.asc(),
        )
        .with_for_update()
    )
    return _result_rows(result)


async def active_traffic_packs(db: AsyncSession, user_id: str, now: datetime | None = None) -> list[TrafficPackBalance]:
    current = now or utcnow()
    result = await db.execute(
        select(TrafficPackBalance)
        .where(
            TrafficPackBalance.user_id == user_id,
            TrafficPackBalance.status == "active",
            TrafficPackBalance.remaining_cents > 0,
            TrafficPackBalance.expires_at > current,
        )
        .order_by(
            TrafficPackBalance.expires_at.asc(),
            TrafficPackBalance.created_at.asc(),
            TrafficPackBalance.id.asc(),
        )
    )
    return _result_rows(result)


async def get_available_balance_cents(
    db: AsyncSession,
    user: User,
    *,
    pending_cost_cents: int = 0,
    now: datetime | None = None,
) -> dict:
    current = now or utcnow()
    sub = await get_subscription(db, user.id)
    changed = normalize_subscription_period(sub, current)
    packs = await active_traffic_packs(db, user.id, current)
    credit_batches = await list_spendable_credit_batches(db, user.id)
    subscription_remaining = available_subscription_cents(sub, current)
    traffic_remaining = sum(max(0, int(pack.remaining_cents or 0)) for pack in packs)
    credit_remaining = sum(max(0, int(batch.remaining_cents or 0)) for batch in credit_batches)
    legacy_balance = int(user.balance or 0)
    available = (
        subscription_remaining
        + traffic_remaining
        + credit_remaining
        + legacy_balance
        - int(pending_cost_cents or 0)
    )
    return {
        "subscription": sub,
        "traffic_packs": packs,
        "subscription_remaining_cents": subscription_remaining,
        "traffic_pack_remaining_cents": traffic_remaining,
        "credit_cents": credit_remaining,
        "legacy_balance_cents": legacy_balance,
        "available_cents": available,
        "changed": changed,
    }


async def debit_usage_cents(
    *,
    db: AsyncSession,
    user: User,
    cost_cents: int,
    source_id: str = "",
    source_type: str = "usage",
    allow_negative_legacy: bool = True,
    reserved_cents: int = 0,
    now: datetime | None = None,
) -> dict:
    amount = max(0, int(cost_cents or 0))
    current = now or utcnow()
    if amount <= 0:
        return {
            "subscription_cents": 0,
            "subscription_id": "",
            "subscription_plan_id": "",
            "traffic_pack_cents": 0,
            "traffic_pack_debits": [],
            "credit_cents": 0,
            "credit_allocations": [],
            "legacy_cents": 0,
        }

    remaining = amount
    subscription_debit = 0
    traffic_debit = 0
    credit_debit = 0
    legacy_debit = 0
    subscription_id = ""
    subscription_plan_id = ""
    traffic_pack_debits = []
    credit_allocations = []

    sub = await get_subscription_for_update(db, user.id)
    subscription_projection = project_subscription_period(sub, current)
    subscription_available = _projected_subscription_available_cents(
        sub,
        subscription_projection,
        current,
    )
    packs = await active_traffic_packs_for_update(db, user.id, current)

    # Callers lock User first. Lock the remaining sources in a stable order and
    # precheck before mutating any debit source so insufficiency is functionally
    # atomic even when the caller inspects the same ORM objects after the error.
    credit_batches = await list_spendable_credit_batches(db, user.id, for_update=True)
    traffic_available = sum(max(0, int(pack.remaining_cents or 0)) for pack in packs)
    credit_available = sum(max(0, int(batch.remaining_cents or 0)) for batch in credit_batches)

    if not allow_negative_legacy:
        total_available = (
            subscription_available
            + traffic_available
            + credit_available
            + int(user.balance or 0)
            - int(reserved_cents or 0)
        )
        if total_available < amount:
            raise BillingError("insufficient balance", status_code=402)

    if sub and subscription_projection:
        _apply_subscription_period_projection(sub, subscription_projection)

    if active_subscription(sub, current):
        take = min(subscription_available, remaining)
        if take > 0:
            sub.used_cents = int(sub.used_cents or 0) + take
            remaining -= take
            subscription_debit = take
            subscription_id = getattr(sub, "id", "") or ""
            subscription_plan_id = getattr(sub, "plan_id", "") or ""
            db.add(_ledger(
                user_id=user.id,
                entry_type="usage_subscription_debit",
                amount_cents=-take,
                source_type=source_type,
                source_id=source_id,
                product_id=sub.plan_id,
                balance_after_cents=available_subscription_cents(sub, current),
            ))

    for pack in packs:
        if remaining <= 0:
            break
        take = min(int(pack.remaining_cents or 0), remaining)
        if take <= 0:
            continue
        pack.remaining_cents = int(pack.remaining_cents or 0) - take
        if pack.remaining_cents <= 0:
            pack.status = "depleted"
        remaining -= take
        traffic_debit += take
        traffic_pack_debits.append({
            "id": getattr(pack, "id", "") or "",
            "product_id": getattr(pack, "product_id", "") or "",
            "cents": take,
        })
        db.add(_ledger(
            user_id=user.id,
            entry_type="usage_traffic_pack_debit",
            amount_cents=-take,
            source_type=source_type,
            source_id=source_id,
            product_id=pack.product_id,
            balance_after_cents=pack.remaining_cents,
        ))

    if remaining > 0 and credit_available > 0:
        credit_debit = min(credit_available, remaining)
        credit_result = await debit_credit_batches(
            db,
            user_id=user.id,
            amount_cents=credit_debit,
        )
        credit_allocations = list(credit_result.get("allocations") or [])
        remaining -= credit_debit

    if remaining > 0:
        user.balance = int(user.balance or 0) - remaining
        legacy_debit = remaining
        db.add(_ledger(
            user_id=user.id,
            entry_type="usage_legacy_balance_debit",
            amount_cents=-remaining,
            source_type=source_type,
            source_id=source_id,
            balance_after_cents=int(user.balance or 0),
        ))

    return {
        "subscription_cents": subscription_debit,
        "subscription_id": subscription_id,
        "subscription_plan_id": subscription_plan_id,
        "traffic_pack_cents": traffic_debit,
        "traffic_pack_debits": traffic_pack_debits,
        "credit_cents": credit_debit,
        "credit_allocations": credit_allocations,
        "legacy_cents": legacy_debit,
    }


def serialize_billing_state(
    sub: UserSubscription | None,
    packs: list[TrafficPackBalance],
    user: User,
    now: datetime | None = None,
    *,
    credit_cents: int | None = None,
) -> dict:
    current = now or utcnow()
    subscription_active = active_subscription(sub, current)
    subscription_remaining = available_subscription_cents(sub, current)
    active_packs = [
        pack for pack in packs
        if getattr(pack, "status", "") == "active"
        and int(getattr(pack, "remaining_cents", 0) or 0) > 0
        and getattr(pack, "expires_at", None)
        and pack.expires_at > current
    ]
    traffic_remaining = sum(int(getattr(pack, "remaining_cents", 0) or 0) for pack in active_packs)
    credit_remaining = int(credit_cents or 0)
    legacy_balance = int(user.balance or 0)
    current_plan = MONTHLY_BY_ID.get(getattr(sub, "plan_id", None)) if sub and getattr(sub, "plan_id", None) else None
    current_rank = current_plan.rank if current_plan and subscription_active else 0
    return {
        "subscription": {
            "active": subscription_active,
            "plan_id": getattr(sub, "plan_id", None) if sub else None,
            "plan_name": current_plan.name if current_plan else None,
            "rank": current_rank,
            "period_start": getattr(sub, "period_start", None).isoformat() if sub and getattr(sub, "period_start", None) else None,
            "period_end": getattr(sub, "period_end", None).isoformat() if sub and getattr(sub, "period_end", None) else None,
            "paid_until": getattr(sub, "paid_until", None).isoformat() if sub and getattr(sub, "paid_until", None) else None,
            "quota_cents": int(getattr(sub, "quota_cents", 0) or 0) if sub else 0,
            "used_cents": int(getattr(sub, "used_cents", 0) or 0) if sub else 0,
            "remaining_cents": subscription_remaining,
            "remaining_usd": cents_to_usd(subscription_remaining),
        },
        "traffic_packs": {
            "remaining_cents": traffic_remaining,
            "remaining_usd": cents_to_usd(traffic_remaining),
            "items": [
                {
                    "id": getattr(pack, "id", ""),
                    "product_id": getattr(pack, "product_id", ""),
                    "remaining_cents": int(getattr(pack, "remaining_cents", 0) or 0),
                    "remaining_usd": cents_to_usd(getattr(pack, "remaining_cents", 0)),
                    "expires_at": getattr(pack, "expires_at", None).isoformat() if getattr(pack, "expires_at", None) else None,
                }
                for pack in active_packs
            ],
            "all_items": [
                {
                    "id": getattr(pack, "id", ""),
                    "product_id": getattr(pack, "product_id", ""),
                    "product_name": PRODUCTS_BY_ID.get(getattr(pack, "product_id", "")).name if PRODUCTS_BY_ID.get(getattr(pack, "product_id", "")) else getattr(pack, "product_id", ""),
                    "status": getattr(pack, "status", ""),
                    "original_cents": int(getattr(pack, "original_cents", 0) or 0),
                    "remaining_cents": int(getattr(pack, "remaining_cents", 0) or 0),
                    "remaining_usd": cents_to_usd(getattr(pack, "remaining_cents", 0)),
                    "expires_at": getattr(pack, "expires_at", None).isoformat() if getattr(pack, "expires_at", None) else None,
                    "created_at": getattr(pack, "created_at", None).isoformat() if getattr(pack, "created_at", None) else None,
                }
                for pack in packs
            ],
        },
        "legacy_balance": {
            "remaining_cents": legacy_balance,
            "remaining_usd": cents_to_usd(legacy_balance),
        },
        "credit_cents": credit_remaining,
        "credit_wallet": {
            "remaining_cents": credit_remaining,
            "remaining_usd": cents_to_usd(credit_remaining),
        },
        # Compatibility alias for clients deployed before credit_wallet became
        # the canonical permanent-credit field.
        "credit_balance": {
            "remaining_cents": credit_remaining,
            "remaining_usd": cents_to_usd(credit_remaining),
        },
        "available": {
            "remaining_cents": subscription_remaining + traffic_remaining + credit_remaining + legacy_balance,
            "remaining_usd": cents_to_usd(
                subscription_remaining + traffic_remaining + credit_remaining + legacy_balance
            ),
        },
        "products": {
            "credits": [serialize_credit_product(product) for product in CREDIT_PRODUCTS],
        },
    }


def serialize_credit_product(product: PaymentProduct) -> dict:
    return {
        "id": product.id,
        "kind": product.kind,
        "name": product.name,
        "money": product.money,
        "price": f"¥{product.money}",
        "amount_fen": product.amount_fen,
        "promised_credit_cents": product.promised_credit_cents,
        "promised_credit_usd": cents_to_usd(product.promised_credit_cents),
        "purchase_action": CREDIT_PURCHASE_ACTION,
        "catalog_version": CREDIT_CATALOG_VERSION,
    }


async def validate_product_purchase(
    *,
    user_id: str,
    product: PaymentProduct,
    money: str,
    db: AsyncSession,
    now: datetime | None = None,
) -> str:
    del user_id, db, now
    if credit_product_by_id(product.id) is None:
        raise BillingError("unknown payment product", status_code=400)
    if normalize_money(money) != format(product.money_decimal, "f"):
        raise BillingError("payment amount does not match selected product", status_code=400)
    return format(product.money_decimal, "f")
