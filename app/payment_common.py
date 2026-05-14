from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .billing import (
    BillingError,
    PAYMENT_PRODUCTS,
    PRODUCTS_BY_ID,
    PRODUCTS_BY_MONEY,
    PaymentProduct,
    apply_payment_product,
    product_by_id,
)
from .finance_summary import ensure_finance_summary_initialized, increment_finance_summary
from .models import PaymentOrder, User
from .referral import process_referral_reward


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
    """Legacy RMB string -> balance cents. Kept for old custom/proof paths."""
    try:
        d = Decimal(str(money_str)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return 0
    if d <= 0:
        return 0
    if d in PRODUCTS_BY_MONEY:
        return PRODUCTS_BY_MONEY[d].balance_cents
    from .config import settings
    rate = Decimal(str(settings.rmb_to_cents_rate))
    return max(1, int((d * rate).to_integral_value(ROUND_DOWN)))


def quote_payment_cents(money_str: str, product_id: Optional[str] = None) -> int:
    if product_id and product_id not in PRODUCTS_BY_ID:
        raise PaymentConfirmError("unknown payment product", status_code=400)
    product = product_by_id(product_id)
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


def _translate_billing_error(exc: BillingError) -> PaymentConfirmError:
    return PaymentConfirmError(exc.detail, status_code=exc.status_code)


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
            "billing_action": "already_confirmed",
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

    add_cents = int(getattr(order, "add_balance_cents", 0) or 0)
    product = product_by_id(getattr(order, "product_id", "") or "")
    billing_action = "legacy_balance_credit"
    subscription = None
    traffic_pack = None
    available_cents = None
    if product:
        try:
            result = await apply_payment_product(user=user, product=product, order_no=order_no, db=db)
        except BillingError as exc:
            raise _translate_billing_error(exc) from exc
        add_cents = int(result.get("added_cents", add_cents) or 0)
        billing_action = str(result.get("billing_action") or product.kind)
        subscription = result.get("subscription")
        traffic_pack = result.get("traffic_pack")
    else:
        if add_cents <= 0:
            add_cents = rmb_to_cents(normalized_money)
            if add_cents <= 0:
                raise PaymentConfirmError("invalid payment amount", status_code=400)
        user.balance += add_cents
        available_cents = int(user.balance or 0)

    order.status = "confirmed"
    order.add_balance_cents = add_cents
    order.trade_no = trade_no or order.trade_no
    order.confirmed_at = datetime.utcnow()

    await ensure_finance_summary_initialized(db, user.id, commit=False)
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
        "billing_action": billing_action,
        "subscription": subscription,
        "traffic_pack": traffic_pack,
        "available_cents": available_cents,
    }
