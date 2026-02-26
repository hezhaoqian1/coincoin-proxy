"""
支付回调 Webhook 接口
- /webhook/recharge        — 旧的手工充值（webhook_secret 鉴权）
- /webhook/pay-notify      — 支付服务异步通知（主路径自动入账）
"""
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .models import PaymentOrder, RechargeLog, User
from .schemas import RechargeRequest, RechargeResponse
from .security import generate_id

router = APIRouter(prefix="/webhook", tags=["webhook"])
logger = logging.getLogger("coincoin.webhook")


def verify_webhook_secret(request: Request):
    """验证 webhook secret"""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing authorization")
    token = auth[7:]
    if token != settings.webhook_secret:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid webhook secret")


@router.post("/recharge", response_model=RechargeResponse, dependencies=[Depends(verify_webhook_secret)])
async def recharge(payload: RechargeRequest, db: AsyncSession = Depends(get_db)):
    """
    充值接口 - 给用户增加 token 和请求额度
    
    - order_id: 外部订单号，用于幂等性（重复调用会返回之前的结果）
    - 用户查找优先级: user_id > username > external_id
    - add_tokens: 增加的 token 额度（会累加到 token_limit）
    - add_daily_requests: 增加的每日请求限额（会累加到 request_limit_per_day）
    """
    # 1. 幂等性检查：如果 order_id 已处理过，直接返回
    existing = await db.execute(
        select(RechargeLog).where(RechargeLog.order_id == payload.order_id)
    )
    existing_log = existing.scalar_one_or_none()
    if existing_log:
        # 已处理过，查询用户当前状态返回
        user_result = await db.execute(select(User).where(User.id == existing_log.user_id))
        user = user_result.scalar_one_or_none()
        return RechargeResponse(
            success=True,
            order_id=payload.order_id,
            user_id=existing_log.user_id,
            balance=user.balance if user else 0,
            token_limit=user.token_limit if user else None,
            request_limit_per_day=user.request_limit_per_day if user else None,
            message="order already processed (idempotent)"
        )

    # 2. 查找用户
    user = None
    if payload.user_id:
        result = await db.execute(select(User).where(User.id == payload.user_id))
        user = result.scalar_one_or_none()
    if not user and payload.username:
        result = await db.execute(select(User).where(User.username == payload.username))
        user = result.scalar_one_or_none()
    if not user and payload.external_id:
        result = await db.execute(select(User).where(User.external_id == payload.external_id))
        user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="user not found, provide valid user_id, username or external_id"
        )

    # 3. 增加额度
    # 增加余额
    if payload.add_balance > 0:
        user.balance += payload.add_balance
    
    # 增加 token 限额（兼容旧逻辑）
    if payload.add_tokens > 0:
        if user.token_limit is None:
            user.token_limit = payload.add_tokens
        else:
            user.token_limit += payload.add_tokens

    if payload.add_daily_requests > 0:
        if user.request_limit_per_day is None:
            user.request_limit_per_day = payload.add_daily_requests
        else:
            user.request_limit_per_day += payload.add_daily_requests

    # 4. 记录充值日志
    log = RechargeLog(
        id=generate_id("r_"),
        order_id=payload.order_id,
        user_id=user.id,
        amount=payload.amount,
        balance_added=payload.add_balance,
        tokens_added=payload.add_tokens,
        daily_requests_added=payload.add_daily_requests,
        note=payload.note,
        created_at=datetime.utcnow(),
    )
    db.add(log)
    await db.commit()

    logger.info(f"Recharge success: order={payload.order_id} user={user.id} balance=+{payload.add_balance} tokens=+{payload.add_tokens} daily_requests=+{payload.add_daily_requests}")

    return RechargeResponse(
        success=True,
        order_id=payload.order_id,
        user_id=user.id,
        balance=user.balance,
        token_limit=user.token_limit,
        request_limit_per_day=user.request_limit_per_day,
        message="recharge success"
    )


@router.get("/recharge/{order_id}", dependencies=[Depends(verify_webhook_secret)])
async def get_recharge(order_id: str, db: AsyncSession = Depends(get_db)):
    """查询充值记录"""
    result = await db.execute(select(RechargeLog).where(RechargeLog.order_id == order_id))
    log = result.scalar_one_or_none()
    if not log:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="order not found")
    
    return {
        "id": log.id,
        "order_id": log.order_id,
        "user_id": log.user_id,
        "amount": log.amount,
        "tokens_added": log.tokens_added,
        "daily_requests_added": log.daily_requests_added,
        "note": log.note,
        "created_at": log.created_at,
    }


# ============================================================
#  支付服务异步通知 — 主路径自动入账
#  支付平台在用户付款后回调此 URL（即使用户关闭浏览器也会到达）
# ============================================================

def _rmb_to_cents(money_str: str) -> int:
    """Mirror of payment.rmb_to_cents (avoid circular import)."""
    PLAN_MAP: dict[Decimal, int] = {
        Decimal("9.90"): 500,
        Decimal("29.90"): 2000,
        Decimal("99.90"): 10000,
    }
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


async def _do_confirm_order(order_no: str, db: AsyncSession) -> bool:
    """
    Core confirm logic shared by pay-notify callback.
    Returns True if balance was added (or already confirmed).
    """
    order = (
        await db.execute(
            select(PaymentOrder)
            .where(PaymentOrder.order_no == order_no)
            .with_for_update()
        )
    ).scalar_one_or_none()

    if not order:
        logger.warning("pay-notify: order %s not found in DB", order_no)
        return False

    if order.status == "confirmed":
        return True

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(f"{settings.pay_base_url}/api/order/{order_no}")
            data = resp.json()
        except Exception as e:
            logger.error("pay-notify: failed to verify order %s: %s", order_no, e)
            return False

    if data.get("status") != 1:
        return False

    money = data.get("money", order.amount_rmb)
    add_cents = _rmb_to_cents(money)
    trade_no = data.get("trade_no", "")

    user = (
        await db.execute(select(User).where(User.id == order.user_id).with_for_update())
    ).scalar_one_or_none()
    if not user:
        logger.error("pay-notify: user %s not found for order %s", order.user_id, order_no)
        return False

    user.balance += add_cents
    order.status = "confirmed"
    order.add_balance_cents = add_cents
    order.trade_no = trade_no
    order.confirmed_at = datetime.utcnow()

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        logger.warning("pay-notify: concurrent confirm for order %s", order_no)
        return True

    logger.info(
        "pay-notify auto-confirmed: order=%s user=%s rmb=%s +%dcents",
        order_no, order.user_id, money, add_cents,
    )
    return True


@router.get("/pay-notify")
async def pay_notify(request: Request, db: AsyncSession = Depends(get_db)):
    """
    支付服务异步回调（GET + query params）。
    标准参数：out_trade_no, trade_no, money, type, name, sign 等。
    我们不信任参数金额 — 拿 out_trade_no 二次查询支付服务确认。
    """
    params = dict(request.query_params)
    order_no = params.get("out_trade_no", "")

    if not order_no:
        return PlainTextResponse("fail", status_code=400)

    logger.info("pay-notify received: order_no=%s params=%s", order_no, params)

    ok = await _do_confirm_order(order_no, db)
    return PlainTextResponse("success" if ok else "fail")
