from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .billing import (
    CREDIT_CATALOGS,
    CREDIT_PURCHASE_ACTION,
    credit_product_by_id,
)
from .credit_wallet import CreditWalletError, grant_permanent_credit
from .finance_summary import ensure_finance_summary_initialized, increment_finance_summary
from .models import PaymentOrder, User
from .referral import process_referral_reward
from .station_settlement import create_station_commission_entry_for_confirmed_order


class PaymentConfirmError(Exception):
    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def normalize_rmb(money_str: str) -> str:
    try:
        value = Decimal(str(money_str))
        normalized = value.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        raise PaymentConfirmError("invalid money amount", status_code=400)
    if not value.is_finite() or value != normalized:
        raise PaymentConfirmError("invalid money amount", status_code=400)
    return format(normalized, "f")


def quote_payment_cents(money_str: str, product_id: Optional[str] = None) -> int:
    product = credit_product_by_id(product_id)
    if product is None:
        raise PaymentConfirmError("unknown payment product", status_code=400)
    if normalize_rmb(money_str) != format(product.money_decimal, "f"):
        raise PaymentConfirmError("payment amount does not match selected product", status_code=400)
    return product.promised_credit_cents


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
        station_result = await create_station_commission_entry_for_confirmed_order(db, order)
        if station_result.created:
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                raise PaymentConfirmError(
                    "payment confirmation conflicted with another update",
                    status_code=409,
                )
        return {
            "success": True,
            "already_confirmed": True,
            "order": order,
            "user": user,
            "amount_rmb": order.amount_rmb,
            "added_cents": order.add_balance_cents,
            "billing_action": "already_confirmed",
            "station_commission": station_result.entry,
        }

    catalog_version = str(getattr(order, "catalog_version", "") or "")
    purchase_action = str(getattr(order, "purchase_action", "") or "")
    product_id = str(getattr(order, "product_id", "") or "")
    promised_credit_cents = getattr(order, "promised_credit_cents", None)
    if (
        not catalog_version
        or catalog_version not in CREDIT_CATALOGS
        or purchase_action != CREDIT_PURCHASE_ACTION
        or credit_product_by_id(product_id, catalog_version) is None
        or isinstance(promised_credit_cents, bool)
        or not isinstance(promised_credit_cents, int)
        or promised_credit_cents <= 0
    ):
        raise PaymentConfirmError(
            "pending order is missing a valid frozen credit commitment",
            status_code=409,
        )

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

    add_cents = promised_credit_cents
    try:
        credit_balance = await grant_permanent_credit(
            db,
            user_id=user.id,
            source_type="payment_order",
            source_id=order.order_no,
            product_id=product_id,
            amount_cents=add_cents,
        )
    except CreditWalletError as exc:
        raise PaymentConfirmError("frozen credit grant conflicted", status_code=409) from exc

    order.status = "confirmed"
    order.add_balance_cents = add_cents
    order.trade_no = trade_no or order.trade_no
    order.confirmed_at = datetime.utcnow()

    station_result = await create_station_commission_entry_for_confirmed_order(db, order)

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
        "billing_action": CREDIT_PURCHASE_ACTION,
        "credit_balance": credit_balance,
        "station_commission": station_result.entry,
        "available_cents": None,
    }
