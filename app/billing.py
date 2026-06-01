from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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


PAYMENT_PRODUCTS: tuple[PaymentProduct, ...] = (
    PaymentProduct("monthly_light", "monthly", "轻量月卡", "49.90", 8000, rank=1),
    PaymentProduct("monthly_basic", "monthly", "基础月卡", "199.00", 40000, rank=2),
    PaymentProduct("monthly_flagship", "monthly", "旗舰月卡", "399.00", 100000, rank=3),
    PaymentProduct("addon_boost", "addon", "补量包", "149.00", 30000, min_plan_rank=1),
    PaymentProduct("addon_project", "addon", "项目包", "399.00", 100000, min_plan_rank=2),
    PaymentProduct("addon_ultra", "addon", "超大包", "699.00", 200000, min_plan_rank=3),
)

PRODUCTS_BY_ID: dict[str, PaymentProduct] = {product.id: product for product in PAYMENT_PRODUCTS}
PRODUCTS_BY_MONEY: dict[Decimal, PaymentProduct] = {}
MONTHLY_PRODUCTS: tuple[PaymentProduct, ...] = tuple(p for p in PAYMENT_PRODUCTS if p.kind == "monthly")
ADDON_PRODUCTS: tuple[PaymentProduct, ...] = tuple(p for p in PAYMENT_PRODUCTS if p.kind == "addon")
MONTHLY_BY_ID: dict[str, PaymentProduct] = {product.id: product for product in MONTHLY_PRODUCTS}
ADDONS_BY_ID: dict[str, PaymentProduct] = {product.id: product for product in ADDON_PRODUCTS}
for product in PAYMENT_PRODUCTS:
    PRODUCTS_BY_MONEY.setdefault(product.money_decimal, product)


def utcnow() -> datetime:
    return datetime.utcnow()


def cents_to_usd(cents: int) -> float:
    return int(cents or 0) / 100


def money_to_rmb_cents(money: str) -> int:
    return int((Decimal(str(money)).quantize(Decimal("0.01")) * 100).to_integral_value())


def rmb_cents_to_money(cents: int) -> str:
    return f"{max(1, int(cents or 0)) / 100:.2f}"


def normalize_money(money: str) -> str:
    try:
        return format(Decimal(str(money)).quantize(Decimal("0.01")), "f")
    except (InvalidOperation, ValueError, TypeError):
        raise BillingError("invalid money amount", status_code=400)


def product_by_id(product_id: Optional[str]) -> Optional[PaymentProduct]:
    return PRODUCTS_BY_ID.get(product_id or "")


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


def normalize_subscription_period(sub: UserSubscription | None, now: datetime | None = None) -> bool:
    if not sub:
        return False
    current = now or utcnow()
    changed = False
    paid_until = getattr(sub, "paid_until", None)
    if not paid_until or paid_until <= current:
        if getattr(sub, "status", "") != "expired":
            sub.status = "expired"
            changed = True
        return changed

    if getattr(sub, "status", "") != "active":
        sub.status = "active"
        changed = True

    if not getattr(sub, "period_start", None) or not getattr(sub, "period_end", None):
        sub.period_start = current
        sub.period_end = _period_end_from(current, paid_until)
        sub.used_cents = 0
        return True

    while getattr(sub, "period_end", None) and sub.period_end <= current and sub.period_end < paid_until:
        sub.period_start = sub.period_end
        sub.period_end = _period_end_from(sub.period_start, paid_until)
        sub.used_cents = 0
        changed = True
    return changed


async def apply_payment_product(
    *,
    user: User,
    product: PaymentProduct,
    order_no: str,
    db: AsyncSession,
    now: datetime | None = None,
) -> dict:
    current = now or utcnow()
    if product.kind == "monthly":
        return await _apply_monthly_product(user=user, product=product, order_no=order_no, db=db, now=current)
    if product.kind == "addon":
        return await _apply_addon_product(user=user, product=product, order_no=order_no, db=db, now=current)
    raise BillingError("unsupported payment product", status_code=400)


async def _apply_monthly_product(
    *,
    user: User,
    product: PaymentProduct,
    order_no: str,
    db: AsyncSession,
    now: datetime,
) -> dict:
    sub = await get_subscription_for_update(db, user.id)
    if sub:
        normalize_subscription_period(sub, now)

    if not active_subscription(sub, now):
        if not sub:
            sub = UserSubscription(id=generate_id("sub_"), user_id=user.id)
            db.add(sub)
        sub.plan_id = product.id
        sub.status = "active"
        sub.period_start = now
        sub.paid_until = now + timedelta(days=BILLING_PERIOD_DAYS)
        sub.period_end = _period_end_from(now, sub.paid_until)
        sub.quota_cents = product.balance_cents
        sub.used_cents = 0
        db.add(_ledger(
            user_id=user.id,
            entry_type="subscription_start",
            amount_cents=product.balance_cents,
            source_type="payment_order",
            source_id=order_no,
            product_id=product.id,
            balance_after_cents=available_subscription_cents(sub, now),
        ))
        return {"billing_action": "subscription_start", "added_cents": product.balance_cents, "subscription": sub}

    current_product = MONTHLY_BY_ID.get(sub.plan_id)
    current_rank = current_product.rank if current_product else 0
    if product.rank < current_rank:
        raise BillingError("cannot purchase a lower tier while a higher subscription is active", status_code=409)

    if product.rank == current_rank:
        if available_subscription_cents(sub, now) <= 0:
            sub.plan_id = product.id
            sub.status = "active"
            sub.period_start = now
            sub.paid_until = now + timedelta(days=BILLING_PERIOD_DAYS)
            sub.period_end = _period_end_from(now, sub.paid_until)
            sub.quota_cents = product.balance_cents
            sub.used_cents = 0
            db.add(_ledger(
                user_id=user.id,
                entry_type="subscription_reset",
                amount_cents=product.balance_cents,
                source_type="payment_order",
                source_id=order_no,
                product_id=product.id,
                balance_after_cents=available_subscription_cents(sub, now),
            ))
            return {"billing_action": "subscription_reset", "added_cents": product.balance_cents, "subscription": sub}

        sub.paid_until = (sub.paid_until or now) + timedelta(days=BILLING_PERIOD_DAYS)
        if sub.period_end:
            sub.period_end = _period_end_from(sub.period_start or now, sub.paid_until)
        db.add(_ledger(
            user_id=user.id,
            entry_type="subscription_renew",
            amount_cents=0,
            source_type="payment_order",
            source_id=order_no,
            product_id=product.id,
            balance_after_cents=available_subscription_cents(sub, now),
        ))
        return {"billing_action": "subscription_renew", "added_cents": 0, "subscription": sub}

    sub.plan_id = product.id
    sub.quota_cents = product.balance_cents
    if sub.period_start:
        sub.period_end = _period_end_from(sub.period_start, sub.paid_until or now)
    db.add(_ledger(
        user_id=user.id,
        entry_type="subscription_upgrade",
        amount_cents=max(0, available_subscription_cents(sub, now)),
        source_type="payment_order",
        source_id=order_no,
        product_id=product.id,
        balance_after_cents=available_subscription_cents(sub, now),
    ))
    return {"billing_action": "subscription_upgrade", "added_cents": max(0, available_subscription_cents(sub, now)), "subscription": sub}


async def _apply_addon_product(
    *,
    user: User,
    product: PaymentProduct,
    order_no: str,
    db: AsyncSession,
    now: datetime,
) -> dict:
    sub = await get_subscription_for_update(db, user.id)
    if sub:
        normalize_subscription_period(sub, now)
    if not active_subscription(sub, now):
        raise BillingError("traffic packs require an active monthly subscription", status_code=409)
    monthly = MONTHLY_BY_ID.get(sub.plan_id)
    current_rank = monthly.rank if monthly else 0
    if current_rank < product.min_plan_rank:
        raise BillingError("traffic pack is not available for the current subscription tier", status_code=409)

    pack = TrafficPackBalance(
        id=generate_id("tp_"),
        user_id=user.id,
        product_id=product.id,
        status="active",
        original_cents=product.balance_cents,
        remaining_cents=product.balance_cents,
        expires_at=now + timedelta(days=TRAFFIC_PACK_VALID_DAYS),
    )
    db.add(pack)
    db.add(_ledger(
        user_id=user.id,
        entry_type="traffic_pack_grant",
        amount_cents=product.balance_cents,
        source_type="payment_order",
        source_id=order_no,
        product_id=product.id,
        balance_after_cents=product.balance_cents,
    ))
    return {"billing_action": "traffic_pack_grant", "added_cents": product.balance_cents, "subscription": sub, "traffic_pack": pack}


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
        .order_by(TrafficPackBalance.expires_at.asc(), TrafficPackBalance.created_at.asc())
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
        .order_by(TrafficPackBalance.expires_at.asc(), TrafficPackBalance.created_at.asc())
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
    subscription_remaining = available_subscription_cents(sub, current)
    traffic_remaining = sum(max(0, int(pack.remaining_cents or 0)) for pack in packs) if active_subscription(sub, current) else 0
    legacy_balance = int(user.balance or 0)
    available = subscription_remaining + traffic_remaining + legacy_balance - int(pending_cost_cents or 0)
    return {
        "subscription": sub,
        "traffic_packs": packs,
        "subscription_remaining_cents": subscription_remaining,
        "traffic_pack_remaining_cents": traffic_remaining,
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
    now: datetime | None = None,
) -> dict:
    amount = max(0, int(cost_cents or 0))
    current = now or utcnow()
    if amount <= 0:
        return {"subscription_cents": 0, "traffic_pack_cents": 0, "legacy_cents": 0}

    remaining = amount
    subscription_debit = 0
    traffic_debit = 0
    legacy_debit = 0

    sub = await get_subscription_for_update(db, user.id)
    if sub:
        normalize_subscription_period(sub, current)
    if active_subscription(sub, current):
        take = min(available_subscription_cents(sub, current), remaining)
        if take > 0:
            sub.used_cents = int(sub.used_cents or 0) + take
            remaining -= take
            subscription_debit = take
            db.add(_ledger(
                user_id=user.id,
                entry_type="usage_subscription_debit",
                amount_cents=-take,
                source_type="usage",
                source_id=source_id,
                product_id=sub.plan_id,
                balance_after_cents=available_subscription_cents(sub, current),
            ))

        packs = await active_traffic_packs_for_update(db, user.id, current)
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
            db.add(_ledger(
                user_id=user.id,
                entry_type="usage_traffic_pack_debit",
                amount_cents=-take,
                source_type="usage",
                source_id=source_id,
                product_id=pack.product_id,
                balance_after_cents=pack.remaining_cents,
            ))

    if remaining > 0:
        user.balance = int(user.balance or 0) - remaining
        legacy_debit = remaining
        db.add(_ledger(
            user_id=user.id,
            entry_type="usage_legacy_balance_debit",
            amount_cents=-remaining,
            source_type="usage",
            source_id=source_id,
            balance_after_cents=int(user.balance or 0),
        ))

    return {
        "subscription_cents": subscription_debit,
        "traffic_pack_cents": traffic_debit,
        "legacy_cents": legacy_debit,
    }


def serialize_billing_state(sub: UserSubscription | None, packs: list[TrafficPackBalance], user: User, now: datetime | None = None) -> dict:
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
    traffic_remaining = sum(int(getattr(pack, "remaining_cents", 0) or 0) for pack in active_packs) if subscription_active else 0
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
        "available": {
            "remaining_cents": subscription_remaining + traffic_remaining + legacy_balance,
            "remaining_usd": cents_to_usd(subscription_remaining + traffic_remaining + legacy_balance),
        },
        "products": {
            "monthly": [serialize_product(product, sub=sub, current_rank=current_rank, now=current) for product in MONTHLY_PRODUCTS],
            "addons": [serialize_product(product, sub=sub, current_rank=current_rank, now=current) for product in ADDON_PRODUCTS],
        },
    }


def serialize_product(
    product: PaymentProduct,
    *,
    sub: UserSubscription | None = None,
    current_rank: int = 0,
    now: datetime | None = None,
) -> dict:
    current = now or utcnow()
    allowed = True
    reason = ""
    if product.kind == "monthly" and current_rank and product.rank < current_rank:
        allowed = False
        reason = "当前套餐高于此档，到期后可重新选择"
    if product.kind == "addon" and current_rank < product.min_plan_rank:
        allowed = False
        reason = "当前套餐暂不可购买此流量包" if current_rank else "流量包仅限有效月卡用户购买"
    pay_money = product.money
    purchase_action = "purchase"
    if product.kind == "monthly" and active_subscription(sub, current):
        current_product = MONTHLY_BY_ID.get(getattr(sub, "plan_id", None))
        current_product_rank = current_product.rank if current_product else 0
        if product.rank == current_product_rank:
            purchase_action = "reset" if available_subscription_cents(sub, current) <= 0 else "renew"
        elif product.rank > current_product_rank:
            purchase_action = "upgrade"
        elif product.rank < current_product_rank:
            purchase_action = "downgrade_blocked"
        if allowed:
            pay_money = quote_product_money(product, sub, current)
    if product.kind == "addon":
        purchase_action = "addon"
    return {
        "id": product.id,
        "kind": product.kind,
        "name": product.name,
        "money": product.money,
        "pay_money": pay_money,
        "price": f"¥{Decimal(pay_money).normalize():f}",
        "balance_cents": product.balance_cents,
        "balance_usd": cents_to_usd(product.balance_cents),
        "rank": product.rank,
        "min_plan_rank": product.min_plan_rank,
        "allowed": allowed,
        "unavailable_reason": reason,
        "purchase_action": purchase_action,
    }


def quote_product_money(product: PaymentProduct, sub: UserSubscription | None, now: datetime | None = None) -> str:
    current = now or utcnow()
    if product.kind == "addon":
        return product.money
    if product.kind != "monthly":
        return product.money

    if not active_subscription(sub, current):
        return product.money

    current_product = MONTHLY_BY_ID.get(getattr(sub, "plan_id", None))
    current_rank = current_product.rank if current_product else 0
    if product.rank < current_rank:
        raise BillingError("cannot purchase a lower tier while a higher subscription is active", status_code=409)
    if product.rank == current_rank:
        return product.money

    period_start = getattr(sub, "period_start", None) or current
    period_end = getattr(sub, "period_end", None) or getattr(sub, "paid_until", None) or (current + timedelta(days=BILLING_PERIOD_DAYS))
    total_seconds = max(1, int((period_end - period_start).total_seconds()))
    remaining_seconds = max(0, int((period_end - current).total_seconds()))
    current_money_cents = money_to_rmb_cents(current_product.money) if current_product else 0
    diff_cents = max(0, money_to_rmb_cents(product.money) - current_money_cents)
    due = (Decimal(diff_cents) * Decimal(remaining_seconds)) / Decimal(total_seconds)
    due_cents = max(1, int(due.quantize(Decimal("1"))))
    return rmb_cents_to_money(due_cents)


async def validate_product_purchase(
    *,
    user_id: str,
    product: PaymentProduct,
    money: str,
    db: AsyncSession,
    now: datetime | None = None,
) -> str:
    current = now or utcnow()
    sub = await get_subscription_for_update(db, user_id)
    if sub:
        normalize_subscription_period(sub, current)

    if product.kind == "addon":
        if not active_subscription(sub, current):
            raise BillingError("traffic packs require an active monthly subscription", status_code=409)
        monthly = MONTHLY_BY_ID.get(getattr(sub, "plan_id", None))
        current_rank = monthly.rank if monthly else 0
        if current_rank < product.min_plan_rank:
            raise BillingError("traffic pack is not available for the current subscription tier", status_code=409)

    expected_money = quote_product_money(product, sub, current)
    if normalize_money(money) != normalize_money(expected_money):
        raise BillingError("payment amount does not match selected product", status_code=400)
    return expected_money
