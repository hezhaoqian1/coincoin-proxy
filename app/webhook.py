"""
支付回调 Webhook 接口
- /webhook/recharge        — 旧的手工充值（webhook_secret 鉴权）
- /webhook/pay-notify      — Epay 异步通知（主路径自动入账）
"""
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .epay import EpayVerificationError, verify_epay_callback_params
from .finance_summary import ensure_finance_summary_initialized, increment_finance_summary
from .models import RechargeLog, User
from .payment_common import PaymentConfirmError, confirm_paid_order
from .schemas import RechargeRequest, RechargeResponse
from .security import generate_id
from .config import settings

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
    existing = await db.execute(
        select(RechargeLog).where(RechargeLog.order_id == payload.order_id)
    )
    existing_log = existing.scalar_one_or_none()
    if existing_log:
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

    if payload.add_balance > 0:
        user.balance += payload.add_balance

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
    await ensure_finance_summary_initialized(db, user.id, commit=False)
    await increment_finance_summary(
        db,
        user.id,
        paid_rmb_cents=int(payload.amount or 0) if payload.amount and payload.add_balance > 0 else 0,
        paid_balance_cents=payload.add_balance if payload.amount and payload.add_balance > 0 else 0,
        ops_credit_cents=payload.add_balance if not payload.amount else 0,
        bonus_cents=0,
        paid_orders=1 if payload.amount and payload.add_balance > 0 else 0,
        payment_at=log.created_at if payload.amount and payload.add_balance > 0 else None,
    )
    await db.commit()

    logger.info(
        "Recharge success: order=%s user=%s balance=+%s tokens=+%s daily_requests=+%s",
        payload.order_id, user.id, payload.add_balance, payload.add_tokens, payload.add_daily_requests,
    )

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
        "balance_added": log.balance_added,
        "tokens_added": log.tokens_added,
        "daily_requests_added": log.daily_requests_added,
        "note": log.note,
        "created_at": log.created_at,
    }


async def _collect_notify_params(request: Request) -> dict[str, str]:
    params: dict[str, str] = dict(request.query_params)
    ct = (request.headers.get("content-type") or "").lower()
    try:
        if "application/json" in ct:
            body = await request.json()
            if isinstance(body, dict):
                for k, v in body.items():
                    params[str(k)] = "" if v is None else str(v)
        elif request.method == "POST":
            form = await request.form()
            for k, v in form.items():
                params[str(k)] = "" if v is None else str(v)
    except Exception:
        pass
    return params


async def _handle_pay_notify(request: Request, db: AsyncSession) -> PlainTextResponse:
    params = await _collect_notify_params(request)
    logger.info("pay-notify received params=%s", params)

    try:
        callback = verify_epay_callback_params(params, require_success=True)
    except EpayVerificationError as exc:
        logger.warning("pay-notify verification failed: %s params=%s", exc.detail, params)
        return PlainTextResponse("fail", status_code=400)

    try:
        result = await confirm_paid_order(
            order_no=callback["out_trade_no"],
            money=callback["money"],
            trade_no=callback["trade_no"],
            db=db,
        )
    except PaymentConfirmError as exc:
        status_code = 409 if exc.status_code == 409 else 400 if exc.status_code == 400 else 404
        logger.warning("pay-notify confirm failed: %s callback=%s", exc.detail, callback)
        return PlainTextResponse("fail", status_code=status_code)

    logger.info(
        "pay-notify confirmed order=%s trade_no=%s already_confirmed=%s",
        callback["out_trade_no"],
        callback["trade_no"],
        result.get("already_confirmed", False),
    )
    return PlainTextResponse("success")


@router.get("/pay-notify")
async def pay_notify(request: Request, db: AsyncSession = Depends(get_db)):
    return await _handle_pay_notify(request, db)


@router.post("/pay-notify")
async def pay_notify_post(request: Request, db: AsyncSession = Depends(get_db)):
    return await _handle_pay_notify(request, db)
