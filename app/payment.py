"""
支付订单：由 CoinCoin 统一创建、验签确认、幂等入账
- POST /v1/orders/create  — 前端带 API Key 调用，proxy 生成订单入库并直连 Epay 生成 pay_url
- POST /v1/orders/confirm — 兜底补单：优先使用支付回跳 proof URL 验签，必要时再尝试查单兜底
"""
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .epay import (
    EpayVerificationError,
    build_epay_submit_url,
    epay_configured,
    extract_epay_params_from_proof_url,
    query_epay_order,
    verify_epay_callback_params,
)
from .billing import BillingError, product_by_id, validate_product_purchase
from .billing import available_subscription_cents, get_available_balance_cents, serialize_billing_state
from .models import PaymentOrder
from .payment_common import PaymentConfirmError, confirm_paid_order, quote_payment_cents
from .proxy import authenticate_user
from .rate_limiter import rate_limiter
from .schemas import (
    OrderConfirmRequest,
    OrderConfirmResponse,
    OrderCreateRequest,
    OrderCreateResponse,
)
from .security import generate_id
from .station_settlement import attach_station_to_order, create_station_commission_entry_for_confirmed_order

router = APIRouter(prefix="/v1", tags=["payment"])
logger = logging.getLogger("coincoin.payment")

CONFIRM_RATE_LIMIT = 30  # per user per minute


def _public_base_url(request: Request) -> str:
    """
    Best-effort public base URL detection.
    Prefer explicit COINCOIN_SELF_BASE_URL. Otherwise honor common reverse-proxy headers.
    """
    if settings.self_base_url:
        base = settings.self_base_url.strip().rstrip("/")
        if "://" not in base:
            base = f"https://{base}"
        return base

    xf_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    xf_host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    host = xf_host or request.headers.get("host", "").strip()

    scheme = xf_proto or request.url.scheme
    if host:
        base = f"{scheme}://{host}".rstrip("/")
        if "://" not in base:
            base = f"https://{host}".rstrip("/")
        return base
    return str(request.base_url).rstrip("/")


def _translate_confirm_error(exc: PaymentConfirmError) -> HTTPException:
    if exc.status_code == 404:
        return HTTPException(status.HTTP_404_NOT_FOUND, exc.detail)
    if exc.status_code == 409:
        return HTTPException(status.HTTP_409_CONFLICT, exc.detail)
    if exc.status_code == 400:
        return HTTPException(status.HTTP_400_BAD_REQUEST, exc.detail)
    return HTTPException(status.HTTP_400_BAD_REQUEST, exc.detail)


async def _confirm_with_query_fallback(order_no: str, db: AsyncSession):
    data = await query_epay_order(order_no)
    if str(data.get("code", "")) == "-1":
        logger.warning("epay query fallback unavailable for %s: %s", order_no, data)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, data.get("msg", "payment query unavailable"))
    trade_status = str(data.get("trade_status", "")).upper()
    if str(data.get("status")) != "1" and trade_status != "TRADE_SUCCESS":
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, "payment not completed")

    trade_no = str(data.get("trade_no", "")).strip()
    money = str(data.get("money", "")).strip()
    if not trade_no or not money:
        logger.warning("epay query fallback missing fields for %s: %s", order_no, data)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "payment query response incomplete")

    try:
        return await confirm_paid_order(
            order_no=order_no,
            money=money,
            trade_no=trade_no,
            db=db,
        )
    except PaymentConfirmError as exc:
        raise _translate_confirm_error(exc) from exc


def _response_from_confirm_result(result: dict, message: str) -> OrderConfirmResponse:
    user = result["user"]
    order = result["order"]
    available_cents = result.get("available_cents")
    return OrderConfirmResponse(
        success=True,
        order_no=order.order_no,
        amount_rmb=result["amount_rmb"],
        added_cents=result["added_cents"],
        new_balance=user.balance,
        new_balance_usd=user.balance / 100,
        available_cents=available_cents,
        available_usd=(available_cents / 100) if available_cents is not None else None,
        billing_action=result.get("billing_action"),
        message=message,
    )


async def _attach_available_balance(result: dict, db: AsyncSession) -> dict:
    user = result.get("user")
    if not user:
        return result
    if result.get("available_cents") is not None:
        return result
    legacy_balance = int(getattr(user, "balance", 0) or 0)
    subscription_remaining = available_subscription_cents(result.get("subscription"))
    traffic_pack = result.get("traffic_pack")
    traffic_remaining = int(getattr(traffic_pack, "remaining_cents", 0) or 0) if traffic_pack else 0
    if subscription_remaining or traffic_remaining:
        result["available_cents"] = subscription_remaining + traffic_remaining + legacy_balance
        return result
    snapshot = await get_available_balance_cents(db, user)
    result["available_cents"] = int(snapshot.get("available_cents", legacy_balance))
    return result


async def _create_station_commission_for_confirmed_order(order_no: str, db: AsyncSession) -> None:
    try:
        refreshed_order_result = await db.execute(select(PaymentOrder).where(PaymentOrder.order_no == order_no))
        refreshed_order = getattr(refreshed_order_result, "scalar_one_or_none", lambda: None)()
        if refreshed_order:
            await create_station_commission_entry_for_confirmed_order(db, refreshed_order)
            await db.commit()
    except Exception:
        logger.exception("failed to create station commission entry for %s", order_no)


async def _confirm_order_with_signed_proof(payload: OrderConfirmRequest, db: AsyncSession) -> dict:
    if not payload.proof_url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "payment proof URL is required")
    try:
        callback_params = verify_epay_callback_params(
            extract_epay_params_from_proof_url(payload.proof_url),
            require_success=True,
        )
    except EpayVerificationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.detail) from exc

    if callback_params["out_trade_no"] != payload.order_no:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "payment proof does not match this order")

    try:
        result = await confirm_paid_order(
            order_no=payload.order_no,
            money=callback_params["money"],
            trade_no=callback_params["trade_no"],
            db=db,
        )
    except PaymentConfirmError as exc:
        raise _translate_confirm_error(exc) from exc

    await _create_station_commission_for_confirmed_order(payload.order_no, db)
    return result


def _payment_order_payload(order: PaymentOrder) -> dict:
    return {
        "id": order.id,
        "order_no": order.order_no,
        "amount_rmb": order.amount_rmb,
        "add_balance_cents": order.add_balance_cents,
        "add_balance_usd": order.add_balance_cents / 100,
        "status": order.status,
        "trade_no": order.trade_no,
        "created_at": order.created_at,
        "confirmed_at": order.confirmed_at,
    }


@router.get("/orders")
async def list_orders(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    cached = await authenticate_user(request, db)
    result = await db.execute(
        select(PaymentOrder)
        .where(PaymentOrder.user_id == cached.id)
        .order_by(PaymentOrder.created_at.desc())
        .limit(limit)
    )
    return [_payment_order_payload(order) for order in result.scalars().all()]


@router.get("/billing/state")
async def billing_state(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    cached = await authenticate_user(request, db)
    snapshot = await get_available_balance_cents(db, cached)
    return serialize_billing_state(
        snapshot.get("subscription"),
        snapshot.get("traffic_packs") or [],
        cached,
    )


@router.post("/orders/create", response_model=OrderCreateResponse)
async def create_order(
    payload: OrderCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    cached = await authenticate_user(request, db)
    user_id = cached.id

    if not epay_configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "payment service is not configured")

    if not await rate_limiter.allow(f"order_create:{user_id}", CONFIRM_RATE_LIMIT):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many order requests")

    try:
        product = product_by_id(payload.product_id)
        if product:
            normalized_money = await validate_product_purchase(
                user_id=user_id,
                product=product,
                money=payload.money,
                db=db,
            )
            payload.money = normalized_money
            expected_cents = product.balance_cents
        else:
            expected_cents = quote_payment_cents(payload.money, payload.product_id)
    except BillingError as exc:
        raise _translate_confirm_error(PaymentConfirmError(exc.detail, status_code=exc.status_code)) from exc
    except PaymentConfirmError as exc:
        raise _translate_confirm_error(exc) from exc
    if expected_cents <= 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid money amount")

    order_no = f"CC_{int(datetime.utcnow().timestamp())}_{generate_id('')[:8]}"

    base = _public_base_url(request)
    notify_url = f"{base}/webhook/pay-notify"
    return_url = f"{base}/pay/return?order_no={order_no}"

    try:
        pay_url = build_epay_submit_url(
            out_trade_no=order_no,
            name=payload.name,
            money=payload.money,
            pay_type=payload.pay_type,
            notify_url=notify_url,
            return_url=return_url,
        )
    except Exception as exc:
        logger.error("Failed to build epay submit URL for %s: %s", order_no, exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "payment service unavailable") from exc

    order = PaymentOrder(
        id=generate_id("po_"),
        user_id=user_id,
        order_no=order_no,
        amount_rmb=payload.money,
        add_balance_cents=expected_cents,
        product_id=payload.product_id or "",
        status="pending",
        pay_url=pay_url,
    )
    try:
        await attach_station_to_order(db, order, user_id)
    except Exception:
        logger.exception("failed to attach station snapshot to order %s", order_no)
    db.add(order)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "duplicate order") from exc

    logger.info(
        "Order created: %s user=%s rmb=%s cents=%d notify=%s return=%s",
        order_no, user_id, payload.money, expected_cents, notify_url, return_url,
    )

    return OrderCreateResponse(
        order_no=order_no,
        pay_url=pay_url,
        amount_rmb=payload.money,
        expected_cents=expected_cents,
    )


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

    payment_order = (
        await db.execute(select(PaymentOrder).where(PaymentOrder.order_no == payload.order_no))
    ).scalar_one_or_none()
    if not payment_order:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "order not found — create via /v1/orders/create first")
    if payment_order.user_id != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "order does not belong to this user")

    if payment_order.status == "confirmed":
        result = {
            "order": payment_order,
            "user": cached,
            "amount_rmb": payment_order.amount_rmb,
            "added_cents": payment_order.add_balance_cents,
            "billing_action": "already_confirmed",
        }
        return _response_from_confirm_result(await _attach_available_balance(result, db), "order already confirmed")

    if payload.proof_url:
        result = await _confirm_order_with_signed_proof(payload, db)
        message = "order already confirmed" if result.get("already_confirmed") else "recharge success"
        return _response_from_confirm_result(await _attach_available_balance(result, db), message)

    try:
        result = await _confirm_with_query_fallback(payload.order_no, db)
    except HTTPException:
        raise
    await _create_station_commission_for_confirmed_order(payload.order_no, db)
    message = "order already confirmed" if result.get("already_confirmed") else "recharge success"
    return _response_from_confirm_result(await _attach_available_balance(result, db), message)


@router.post("/orders/confirm-return", response_model=OrderConfirmResponse)
async def confirm_order_from_return(
    payload: OrderConfirmRequest,
    db: AsyncSession = Depends(get_db),
):
    result = await _confirm_order_with_signed_proof(payload, db)
    message = "order already confirmed" if result.get("already_confirmed") else "recharge success"
    return _response_from_confirm_result(await _attach_available_balance(result, db), message)
