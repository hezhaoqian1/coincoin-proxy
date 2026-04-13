from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .models import PaymentOrder, User
from .referral import process_referral_reward

PLAN_MAP: dict[Decimal, int] = {
    Decimal("9.90"):   4999,    # 体验包  $49.99
    Decimal("29.90"):  14999,   # 轻量版  $149.99
    Decimal("59.90"):  29999,   # 基础版  $299.99
    Decimal("99.90"):  49999,   # 进阶版  $499.99
    Decimal("199.90"): 99999,   # 专业版  $999.99
    Decimal("499.90"): 249999,  # 旗舰版  $2499.99
}


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
    if d in PLAN_MAP:
        return PLAN_MAP[d]
    rate = Decimal(str(settings.rmb_to_cents_rate))
    return max(1, int((d * rate).to_integral_value(ROUND_DOWN)))


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

    user.balance += add_cents
    order.status = "confirmed"
    order.add_balance_cents = add_cents
    order.trade_no = trade_no or order.trade_no
    order.confirmed_at = datetime.utcnow()

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
