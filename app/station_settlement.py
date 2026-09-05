from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .models import PaymentOrder, Station, StationCommissionLedgerEntry, StationCustomerLink
from .security import generate_id


@dataclass(frozen=True)
class StationCommissionEnsureResult:
    entry: StationCommissionLedgerEntry | None
    created: bool


def rmb_to_minor_cents_safe(money_str: str) -> int:
    try:
        d = Decimal(str(money_str)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return 0
    if d <= 0:
        return 0
    return int((d * 100).to_integral_value(ROUND_DOWN))


async def attach_station_to_order(db: AsyncSession, order: PaymentOrder, user_id: str) -> None:
    if getattr(order, "station_id", None):
        return

    link = (
        await db.execute(
            select(StationCustomerLink)
            .where(
                StationCustomerLink.user_id == user_id,
                StationCustomerLink.status == "active",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if not link:
        return

    station = (
        await db.execute(
            select(Station)
            .where(
                Station.id == link.station_id,
                Station.status == "active",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if not station:
        return

    order.station_id = station.id
    order.station_owner_user_id = station.owner_user_id
    order.station_commission_rate = float(getattr(station, "commission_rate", 0.0) or 0.0)
    order.station_payout_status = "pending"


async def create_station_commission_entry_for_confirmed_order(
    db: AsyncSession,
    order: PaymentOrder,
) -> StationCommissionEnsureResult:
    station_id = getattr(order, "station_id", None)
    if not station_id or order.status != "confirmed":
        return StationCommissionEnsureResult(entry=None, created=False)

    existing = (
        await db.execute(
            select(StationCommissionLedgerEntry)
            .where(StationCommissionLedgerEntry.payment_order_id == order.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing:
        return StationCommissionEnsureResult(entry=existing, created=False)

    gross_rmb_cents = rmb_to_minor_cents_safe(order.amount_rmb)
    commission_rate = float(getattr(order, "station_commission_rate", 0.0) or 0.0)
    commission_rmb_cents = max(0, int(gross_rmb_cents * commission_rate))
    hold_until = datetime.utcnow() + timedelta(days=max(0, int(settings.station_payout_hold_days)))

    entry = StationCommissionLedgerEntry(
        id=generate_id("scl_"),
        station_id=station_id,
        user_id=order.user_id,
        payment_order_id=order.id,
        order_no=order.order_no,
        status="pending",
        settlement_method="alipay_manual",
        gross_rmb_cents=gross_rmb_cents,
        commission_rate=commission_rate,
        commission_rmb_cents=commission_rmb_cents,
        hold_until=hold_until,
        note="auto-created from confirmed payment order",
    )
    db.add(entry)

    order.station_commission_rmb_cents = commission_rmb_cents
    order.station_payout_status = "pending"
    return StationCommissionEnsureResult(entry=entry, created=True)
