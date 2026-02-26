"""
支付订单：由 proxy 统一创建、确认
- POST /v1/orders/create  — 前端带 API Key 调用，proxy 生成订单入库后转发支付服务拿 pay_url
- POST /v1/orders/confirm — 兜底补单：校验归属 + 二次验证 + FOR UPDATE 防并发
"""
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .models import PaymentOrder, User
from .proxy import authenticate_user
from .rate_limiter import rate_limiter
from .schemas import (
    OrderConfirmRequest,
    OrderConfirmResponse,
    OrderCreateRequest,
    OrderCreateResponse,
)
from .security import generate_id

router = APIRouter(prefix="/v1", tags=["payment"])
logger = logging.getLogger("coincoin.payment")

PLAN_MAP: dict[Decimal, int] = {
    Decimal("9.90"): 500,
    Decimal("29.90"): 2000,
    Decimal("99.90"): 10000,
}

CONFIRM_RATE_LIMIT = 6  # per user per minute


def rmb_to_cents(money_str: str) -> int:
    """RMB string → balance cents.  Uses Decimal to avoid float rounding."""
    try:
        d = Decimal(money_str).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return 0
    if d <= 0:
        return 0
    if d in PLAN_MAP:
        return PLAN_MAP[d]
    rate = Decimal(str(settings.rmb_to_cents_rate))
    return max(1, int((d * rate).to_integral_value(ROUND_DOWN)))


# ---------------------------------------------------------------------------
# POST /v1/orders/create  —  前端唯一的下单入口
# ---------------------------------------------------------------------------
@router.post("/orders/create", response_model=OrderCreateResponse)
async def create_order(
    payload: OrderCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    cached = await authenticate_user(request, db)
    user_id = cached.id

    if not await rate_limiter.allow(f"order_create:{user_id}", CONFIRM_RATE_LIMIT):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many order requests")

    expected_cents = rmb_to_cents(payload.money)
    if expected_cents <= 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid money amount")

    order_no = f"CC_{int(datetime.utcnow().timestamp())}_{generate_id('')[:8]}"

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.post(
                f"{settings.pay_base_url}/api/pay",
                json={
                    "out_trade_no": order_no,
                    "name": payload.name,
                    "money": payload.money,
                    "type": payload.pay_type,
                    "sitename": "CoinCoin",
                    "notify_url": f"{settings.self_base_url or str(request.base_url).rstrip('/')}/webhook/pay-notify",
                },
            )
            data = resp.json()
        except Exception as e:
            logger.error("Failed to create pay order: %s", e)
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "payment service unavailable")

    if data.get("code") != 1 or not data.get("pay_url"):
        logger.warning("Pay service rejected order: %s", data)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "payment service error")

    pay_url = data["pay_url"]
    final_order_no = data.get("out_trade_no", order_no)

    order = PaymentOrder(
        id=generate_id("po_"),
        user_id=user_id,
        order_no=final_order_no,
        amount_rmb=payload.money,
        add_balance_cents=expected_cents,
        status="pending",
        pay_url=pay_url,
    )
    db.add(order)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "duplicate order")

    logger.info("Order created: %s user=%s rmb=%s cents=%d", final_order_no, user_id, payload.money, expected_cents)

    return OrderCreateResponse(
        order_no=final_order_no,
        pay_url=pay_url,
        amount_rmb=payload.money,
        expected_cents=expected_cents,
    )


# ---------------------------------------------------------------------------
# POST /v1/orders/confirm  —  兜底补单（前端 PayReturn 调用）
# ---------------------------------------------------------------------------
@router.post("/orders/confirm", response_model=OrderConfirmResponse)
async def confirm_order(
    payload: OrderConfirmRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    cached = await authenticate_user(request, db)
    user_id = cached.id

    if not await rate_limiter.allow(f"order_confirm:{user_id}", CONFIRM_RATE_LIMIT):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many confirm requests")

    order = (
        await db.execute(
            select(PaymentOrder)
            .where(PaymentOrder.order_no == payload.order_no)
            .with_for_update()
        )
    ).scalar_one_or_none()

    if not order:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "order not found — create via /v1/orders/create first")
    if order.user_id != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "order does not belong to this user")

    if order.status == "confirmed":
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        return OrderConfirmResponse(
            success=True,
            order_no=order.order_no,
            amount_rmb=order.amount_rmb,
            added_cents=order.add_balance_cents,
            new_balance=user.balance,
            new_balance_usd=user.balance / 100,
            message="order already confirmed",
        )

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(f"{settings.pay_base_url}/api/order/{payload.order_no}")
            data = resp.json()
        except Exception as e:
            logger.error("Failed to verify order %s: %s", payload.order_no, e)
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "unable to verify payment status")

    if data.get("status") != 1:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, "payment not completed")

    money = data.get("money", order.amount_rmb)
    add_cents = rmb_to_cents(money)
    trade_no = data.get("trade_no", "")

    user = (
        await db.execute(select(User).where(User.id == user_id).with_for_update())
    ).scalar_one()
    user.balance += add_cents

    order.status = "confirmed"
    order.add_balance_cents = add_cents
    order.trade_no = trade_no
    order.confirmed_at = datetime.utcnow()

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        logger.warning("Concurrent confirm caught by DB for order %s", payload.order_no)
        return OrderConfirmResponse(
            success=True,
            order_no=payload.order_no,
            amount_rmb=money,
            added_cents=add_cents,
            new_balance=user.balance,
            new_balance_usd=user.balance / 100,
            message="order already confirmed (concurrent)",
        )

    logger.info(
        "Payment confirmed: order=%s user=%s rmb=%s +%dcents balance=%d",
        payload.order_no, user_id, money, add_cents, user.balance,
    )

    return OrderConfirmResponse(
        success=True,
        order_no=payload.order_no,
        amount_rmb=money,
        added_cents=add_cents,
        new_balance=user.balance,
        new_balance_usd=user.balance / 100,
        message="recharge success",
    )
