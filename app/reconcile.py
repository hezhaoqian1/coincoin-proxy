import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import SessionLocal
from .models import PaymentOrder
from .webhook import _do_confirm_order

logger = logging.getLogger("coincoin.reconcile")


async def reconcile_once(max_orders: int = 20, lookback_hours: int = 72) -> int:
    """
    Best-effort background reconciliation:
    - Find recent pending orders
    - Verify with payment service and credit if already paid
    Returns number of orders confirmed (including "already confirmed" treated as success).
    """
    cutoff = datetime.utcnow() - timedelta(hours=lookback_hours)
    confirmed = 0

    async with SessionLocal() as db:  # type: AsyncSession
        rows = await db.execute(
            select(PaymentOrder.order_no)
            .where(PaymentOrder.status == "pending")
            .where(PaymentOrder.created_at >= cutoff)
            .order_by(PaymentOrder.created_at.asc())
            .limit(max_orders)
        )
        order_nos = list(rows.scalars().all())

        for order_no in order_nos:
            try:
                ok = await _do_confirm_order(order_no, db)
                confirmed += 1 if ok else 0
            except Exception as e:
                logger.warning("reconcile: failed for order %s: %s", order_no, e)
                # Continue to next order.

    return confirmed


async def reconcile_loop(interval_seconds: int = 60) -> None:
    """
    Periodically reconcile pending orders. This is a safety net for missed callbacks
    and for users closing the browser before the frontend confirms the payment.
    """
    # Small startup delay so app can finish boot and DB can be ready.
    await asyncio.sleep(2)
    while True:
        try:
            n = await reconcile_once()
            if n:
                logger.info("reconcile: confirmed %d pending order(s)", n)
        except Exception as e:
            logger.warning("reconcile loop error: %s", e)
        await asyncio.sleep(max(10, int(interval_seconds)))

