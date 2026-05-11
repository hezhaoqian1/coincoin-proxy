from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .finance_summary import ensure_finance_summary_initialized, increment_finance_summary
from .models import PaymentOrder, User
from .referral import process_referral_reward

@dataclass(frozen=True)
class PaymentProduct:
    id: str
    kind: str
    name: str
    money: str
    balance_cents: int

    @property
    def money_decimal(self) -> Decimal:
        return Decimal(self.money).quantize(Decimal("0.01"))


PAYMENT_PRODUCTS: tuple[PaymentProduct, ...] = (
    PaymentProduct("monthly_light", "monthly", "轻量月卡", "29.90", 7500),
    PaymentProduct("monthly_basic", "monthly", "基础月卡", "129.00", 38000),
    PaymentProduct("monthly_flagship", "monthly", "旗舰月卡", "299.00", 100000),
    PaymentProduct("addon_boost", "addon", "补量包", "99.00", 25000),
    PaymentProduct("addon_project", "addon", "项目包", "249.00", 78000),
    PaymentProduct("addon_ultra", "addon", "超大包", "499.00", 200000),
)

PRODUCTS_BY_ID: dict[str, PaymentProduct] = {product.id: product for product in PAYMENT_PRODUCTS}
PRODUCTS_BY_MONEY: dict[Decimal, PaymentProduct] = {}
for product in PAYMENT_PRODUCTS:
    PRODUCTS_BY_MONEY.setdefault(product.money_decimal, product)


class PaymentConfirmError(Exception):
    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def normalize_rmb(money_str: str) -> str:
    try:
        return format(Decimal(str(money_str)).quantize(Decimal("0.01")), "f")
    except (InvalidOperation, ValueError, TypeError):
        raise PaymentConfirmError("invalid money amount", status_code=400)


def rmb_to_cents(money_str: str) -> int:
    """RMB string → balance cents. Uses Decimal to avoid float rounding."""
    try:
        d = Decimal(str(money_str)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return 0
    if d <= 0:
        return 0
    if d in PRODUCTS_BY_MONEY:
        return PRODUCTS_BY_MONEY[d].balance_cents
    rate = Decimal(str(settings.rmb_to_cents_rate))
    return max(1, int((d * rate).to_integral_value(ROUND_DOWN)))


def quote_payment_cents(money_str: str, product_id: Optional[str] = None) -> int:
    if product_id and product_id not in PRODUCTS_BY_ID:
        raise PaymentConfirmError("unknown payment product", status_code=400)
    product = PRODUCTS_BY_ID.get(product_id or "")
    if product:
        if normalize_rmb(money_str) != format(product.money_decimal, "f"):
            raise PaymentConfirmError("payment amount does not match selected product", status_code=400)
        return product.balance_cents
    return rmb_to_cents(money_str)


def rmb_to_minor_cents(money_str: str) -> int:
    """RMB string -> RMB cents for finance reporting."""
    try:
        d = Decimal(str(money_str)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return 0
    if d <= 0:
        return 0
    return int((d * 100).to_integral_value(ROUND_DOWN))


async def confirm_paid_order(
    *,
    order_no: str,
    money: str,
    trade_no: str,
    db: AsyncSession,
):
    normalized_money = normalize_rmb(money)

    order = (
        await db.execute(
            select(PaymentOrder)
            .where(PaymentOrder.order_no == order_no)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not order:
        raise PaymentConfirmError("order not found", status_code=404)

    user = (
        await db.execute(select(User).where(User.id == order.user_id).with_for_update())
    ).scalar_one_or_none()
    if not user:
        raise PaymentConfirmError("user not found for order", status_code=404)

    if order.status == "confirmed":
        return {
            "success": True,
            "already_confirmed": True,
            "order": order,
            "user": user,
            "amount_rmb": order.amount_rmb,
            "added_cents": order.add_balance_cents,
        }

    if normalized_money != normalize_rmb(order.amount_rmb):
        raise PaymentConfirmError("payment amount does not match order amount", status_code=400)

    duplicate_trade = None
    if trade_no:
        duplicate_trade = (
            await db.execute(
                select(PaymentOrder.order_no)
                .where(PaymentOrder.trade_no == trade_no)
                .where(PaymentOrder.order_no != order_no)
            )
        ).scalar_one_or_none()
    if duplicate_trade:
        raise PaymentConfirmError(f"trade_no already linked to order {duplicate_trade}", status_code=409)

    # The quoted balance is locked in when the order is created.
    # Confirming an old pending order must not pick up a newer pricing table.
    add_cents = int(getattr(order, "add_balance_cents", 0) or 0)
    if add_cents <= 0:
        add_cents = rmb_to_cents(normalized_money)
        if add_cents <= 0:
            raise PaymentConfirmError("invalid payment amount", status_code=400)

    await ensure_finance_summary_initialized(db, user.id, commit=False)
    user.balance += add_cents
    order.status = "confirmed"
    order.add_balance_cents = add_cents
    order.trade_no = trade_no or order.trade_no
    order.confirmed_at = datetime.utcnow()

    await increment_finance_summary(
        db,
        user.id,
        paid_rmb_cents=rmb_to_minor_cents(normalized_money),
        paid_balance_cents=add_cents,
        paid_orders=1,
        payment_at=order.confirmed_at,
    )

    await process_referral_reward(user, add_cents, order_no, db)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise PaymentConfirmError("payment confirmation conflicted with another update", status_code=409)

    return {
        "success": True,
        "already_confirmed": False,
        "order": order,
        "user": user,
        "amount_rmb": normalized_money,
        "added_cents": add_cents,
    }
