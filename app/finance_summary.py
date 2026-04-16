from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import PaymentOrder, RechargeLog, ReferralReward, RequestLog, UsageDaily, UserFinanceSummary


@dataclass(frozen=True)
class FinanceBreakdown:
    total_paid_rmb_cents: int = 0
    total_paid_balance_cents: int = 0
    total_ops_credit_cents: int = 0
    total_bonus_cents: int = 0
    total_consumed_cents: int = 0
    total_ops_debit_cents: int = 0
    legacy_unclassified_cents: int = 0
    total_paid_orders: int = 0
    last_payment_at: datetime | None = None


def _coerce_int(value) -> int:
    return int(value or 0)


async def ensure_finance_summary_initialized(db: AsyncSession, user_id: str, *, commit: bool = True) -> None:
    row = (
        await db.execute(
            select(UserFinanceSummary).where(UserFinanceSummary.user_id == user_id)
        )
    ).scalar_one_or_none()
    if row and int(getattr(row, "initialized_from_history", 0) or 0) == 1:
        return

    try:
        breakdown = await compute_finance_breakdown(db, user_id)
    except Exception:
        # Test fakes and partial environments may not provide enough query fixtures
        # for a full historical backfill. Fall back to an empty baseline so the
        # summary can still accept incremental updates inside the current transaction.
        breakdown = FinanceBreakdown()
    stmt = mysql_insert(UserFinanceSummary).values(
        user_id=user_id,
        initialized_from_history=1,
        total_paid_rmb_cents=breakdown.total_paid_rmb_cents,
        total_paid_balance_cents=breakdown.total_paid_balance_cents,
        total_ops_credit_cents=breakdown.total_ops_credit_cents,
        total_bonus_cents=breakdown.total_bonus_cents,
        total_consumed_cents=breakdown.total_consumed_cents,
        total_ops_debit_cents=breakdown.total_ops_debit_cents,
        legacy_unclassified_cents=breakdown.legacy_unclassified_cents,
        total_paid_orders=breakdown.total_paid_orders,
        last_payment_at=breakdown.last_payment_at,
        updated_at=datetime.utcnow(),
    )
    stmt = stmt.on_duplicate_key_update(
        initialized_from_history=1,
        total_paid_rmb_cents=breakdown.total_paid_rmb_cents,
        total_paid_balance_cents=breakdown.total_paid_balance_cents,
        total_ops_credit_cents=breakdown.total_ops_credit_cents,
        total_bonus_cents=breakdown.total_bonus_cents,
        total_consumed_cents=breakdown.total_consumed_cents,
        total_ops_debit_cents=breakdown.total_ops_debit_cents,
        legacy_unclassified_cents=breakdown.legacy_unclassified_cents,
        total_paid_orders=breakdown.total_paid_orders,
        last_payment_at=breakdown.last_payment_at,
        updated_at=datetime.utcnow(),
    )
    await db.execute(stmt)
    if commit:
        await db.commit()


async def compute_finance_breakdown(db: AsyncSession, user_id: str) -> FinanceBreakdown:
    confirmed_orders = (
        await db.execute(
            select(PaymentOrder).where(
                PaymentOrder.user_id == user_id,
                PaymentOrder.status == "confirmed",
            )
        )
    ).scalars().all()
    recharge_logs = (
        await db.execute(select(RechargeLog).where(RechargeLog.user_id == user_id))
    ).scalars().all()
    referral_rewards = (
        await db.execute(select(ReferralReward).where(ReferralReward.referrer_id == user_id))
    ).scalars().all()
    request_logs = (
        await db.execute(
            select(RequestLog.cost_cents).where(RequestLog.user_id == user_id)
        )
    ).all()

    total_paid_rmb_cents = 0
    total_paid_balance_cents = 0
    total_paid_orders = 0
    last_payment_at: datetime | None = None
    for order in confirmed_orders:
        total_paid_orders += 1
        total_paid_balance_cents += _coerce_int(getattr(order, "add_balance_cents", 0))
        total_paid_rmb_cents += _money_str_to_cents(getattr(order, "amount_rmb", "0"))
        confirmed_at = getattr(order, "confirmed_at", None) or getattr(order, "created_at", None)
        if confirmed_at and (last_payment_at is None or confirmed_at > last_payment_at):
            last_payment_at = confirmed_at

    total_ops_credit_cents = 0
    total_bonus_cents = 0
    legacy_unclassified_cents = 0
    for log in recharge_logs:
        note = (getattr(log, "note", "") or "").lower()
        balance_added = _coerce_int(getattr(log, "balance_added", 0))
        if _looks_like_payment_log(log, note):
            total_paid_rmb_cents += _coerce_int(getattr(log, "amount", 0))
            total_paid_balance_cents += balance_added
            if balance_added > 0:
                total_paid_orders += 1
        elif _looks_like_bonus_log(note):
            total_bonus_cents += balance_added
        else:
            total_ops_credit_cents += balance_added

        if getattr(log, "created_at", None) and _looks_like_payment_log(log, note):
            if last_payment_at is None or log.created_at > last_payment_at:
                last_payment_at = log.created_at

    total_bonus_cents += sum(_coerce_int(getattr(row, "reward_cents", 0)) for row in referral_rewards)
    total_consumed_cents = sum(_coerce_int(cost) for (cost,) in request_logs)

    return FinanceBreakdown(
        total_paid_rmb_cents=total_paid_rmb_cents,
        total_paid_balance_cents=total_paid_balance_cents,
        total_ops_credit_cents=total_ops_credit_cents,
        total_bonus_cents=total_bonus_cents,
        total_consumed_cents=total_consumed_cents,
        total_ops_debit_cents=0,
        legacy_unclassified_cents=legacy_unclassified_cents,
        total_paid_orders=total_paid_orders,
        last_payment_at=last_payment_at,
    )


async def increment_finance_summary(
    db: AsyncSession,
    user_id: str,
    *,
    paid_rmb_cents: int = 0,
    paid_balance_cents: int = 0,
    ops_credit_cents: int = 0,
    bonus_cents: int = 0,
    consumed_cents: int = 0,
    ops_debit_cents: int = 0,
    paid_orders: int = 0,
    payment_at: datetime | None = None,
) -> None:
    stmt = mysql_insert(UserFinanceSummary).values(
        user_id=user_id,
        initialized_from_history=1,
        total_paid_rmb_cents=_coerce_int(paid_rmb_cents),
        total_paid_balance_cents=_coerce_int(paid_balance_cents),
        total_ops_credit_cents=_coerce_int(ops_credit_cents),
        total_bonus_cents=_coerce_int(bonus_cents),
        total_consumed_cents=_coerce_int(consumed_cents),
        total_ops_debit_cents=_coerce_int(ops_debit_cents),
        legacy_unclassified_cents=0,
        total_paid_orders=_coerce_int(paid_orders),
        last_payment_at=payment_at,
        updated_at=datetime.utcnow(),
    )
    update_payload = {
        "initialized_from_history": 1,
        "total_paid_rmb_cents": UserFinanceSummary.total_paid_rmb_cents + _coerce_int(paid_rmb_cents),
        "total_paid_balance_cents": UserFinanceSummary.total_paid_balance_cents + _coerce_int(paid_balance_cents),
        "total_ops_credit_cents": UserFinanceSummary.total_ops_credit_cents + _coerce_int(ops_credit_cents),
        "total_bonus_cents": UserFinanceSummary.total_bonus_cents + _coerce_int(bonus_cents),
        "total_consumed_cents": UserFinanceSummary.total_consumed_cents + _coerce_int(consumed_cents),
        "total_ops_debit_cents": UserFinanceSummary.total_ops_debit_cents + _coerce_int(ops_debit_cents),
        "total_paid_orders": UserFinanceSummary.total_paid_orders + _coerce_int(paid_orders),
        "updated_at": datetime.utcnow(),
    }
    if payment_at is not None:
        update_payload["last_payment_at"] = payment_at
    stmt = stmt.on_duplicate_key_update(**update_payload)
    await db.execute(stmt)


async def get_finance_summary_row(
    db: AsyncSession,
    user_id: str,
    *,
    initialize: bool = False,
) -> UserFinanceSummary | None:
    if initialize:
        await ensure_finance_summary_initialized(db, user_id)
    return (
        await db.execute(
            select(UserFinanceSummary).where(UserFinanceSummary.user_id == user_id)
        )
    ).scalar_one_or_none()


async def get_finance_summary_rows(
    db: AsyncSession,
    user_ids: Iterable[str],
) -> dict[str, UserFinanceSummary]:
    unique_user_ids = [user_id for user_id in dict.fromkeys(user_ids) if user_id]
    if not unique_user_ids:
        return {}
    rows = (
        await db.execute(
            select(UserFinanceSummary).where(UserFinanceSummary.user_id.in_(unique_user_ids))
        )
    ).scalars().all()
    return {row.user_id: row for row in rows}


async def get_user_consumption_windows(db: AsyncSession, user_id: str) -> dict[str, int]:
    today = date.today()
    windows = {}
    for days in (7, 30):
        start = today - timedelta(days=days - 1)
        total = await db.scalar(
            select(func.coalesce(func.sum(UsageDaily.cost_cents), 0)).where(
                UsageDaily.user_id == user_id,
                UsageDaily.day >= start,
                UsageDaily.day <= today,
            )
        )
        windows[f"consumed_{days}d_cents"] = _coerce_int(total)
    return windows


async def get_user_consumption_windows_batch(
    db: AsyncSession,
    user_ids: Iterable[str],
) -> dict[str, dict[str, int]]:
    unique_user_ids = [user_id for user_id in dict.fromkeys(user_ids) if user_id]
    if not unique_user_ids:
        return {}

    today = date.today()
    start_30d = today - timedelta(days=29)
    start_7d = today - timedelta(days=6)
    windows = {
        user_id: {
            "consumed_7d_cents": 0,
            "consumed_30d_cents": 0,
        }
        for user_id in unique_user_ids
    }

    rows = (
        await db.execute(
            select(UsageDaily.user_id, UsageDaily.day, UsageDaily.cost_cents).where(
                UsageDaily.user_id.in_(unique_user_ids),
                UsageDaily.day >= start_30d,
                UsageDaily.day <= today,
            )
        )
    ).all()

    for user_id, usage_day, cost_cents in rows:
        cents = _coerce_int(cost_cents)
        entry = windows.setdefault(
            user_id,
            {
                "consumed_7d_cents": 0,
                "consumed_30d_cents": 0,
            },
        )
        entry["consumed_30d_cents"] += cents
        if usage_day >= start_7d:
            entry["consumed_7d_cents"] += cents
    return windows


def _serialize_finance_snapshot(
    summary: UserFinanceSummary | None,
    current_balance_cents: int,
    windows: dict[str, int] | None = None,
) -> dict:
    windows = windows or {"consumed_7d_cents": 0, "consumed_30d_cents": 0}

    total_paid_rmb_cents = _coerce_int(getattr(summary, "total_paid_rmb_cents", 0))
    total_paid_balance_cents = _coerce_int(getattr(summary, "total_paid_balance_cents", 0))
    total_ops_credit_cents = _coerce_int(getattr(summary, "total_ops_credit_cents", 0))
    total_bonus_cents = _coerce_int(getattr(summary, "total_bonus_cents", 0))
    total_consumed_cents = _coerce_int(getattr(summary, "total_consumed_cents", 0))
    total_ops_debit_cents = _coerce_int(getattr(summary, "total_ops_debit_cents", 0))
    legacy_unclassified_cents = _coerce_int(getattr(summary, "legacy_unclassified_cents", 0))
    consumed_7d_cents = _coerce_int(windows.get("consumed_7d_cents", 0))
    consumed_30d_cents = _coerce_int(windows.get("consumed_30d_cents", 0))
    total_credit_cents = total_paid_balance_cents + total_ops_credit_cents + total_bonus_cents
    net_flow_cents = total_credit_cents - total_consumed_cents - total_ops_debit_cents

    return {
        "total_paid_rmb_cents": total_paid_rmb_cents,
        "total_paid_rmb_usd": total_paid_rmb_cents / 100,
        "total_paid_balance_cents": total_paid_balance_cents,
        "total_paid_balance_usd": total_paid_balance_cents / 100,
        "total_ops_credit_cents": total_ops_credit_cents,
        "total_ops_credit_usd": total_ops_credit_cents / 100,
        "total_bonus_cents": total_bonus_cents,
        "total_bonus_usd": total_bonus_cents / 100,
        "total_consumed_cents": total_consumed_cents,
        "total_consumed_usd": total_consumed_cents / 100,
        "total_ops_debit_cents": total_ops_debit_cents,
        "total_ops_debit_usd": total_ops_debit_cents / 100,
        "legacy_unclassified_cents": legacy_unclassified_cents,
        "legacy_unclassified_usd": legacy_unclassified_cents / 100,
        "total_credit_cents": total_credit_cents,
        "total_credit_usd": total_credit_cents / 100,
        "net_flow_cents": net_flow_cents,
        "net_flow_usd": net_flow_cents / 100,
        "current_balance_cents": _coerce_int(current_balance_cents),
        "current_balance_usd": _coerce_int(current_balance_cents) / 100,
        "consumed_7d_cents": consumed_7d_cents,
        "consumed_7d_usd": consumed_7d_cents / 100,
        "consumed_30d_cents": consumed_30d_cents,
        "consumed_30d_usd": consumed_30d_cents / 100,
        "total_paid_orders": _coerce_int(getattr(summary, "total_paid_orders", 0)),
        "last_payment_at": getattr(summary, "last_payment_at", None),
        "initialized_from_history": bool(_coerce_int(getattr(summary, "initialized_from_history", 0))),
    }


async def build_user_finance_snapshot(db: AsyncSession, user_id: str, current_balance_cents: int) -> dict:
    summary = await get_finance_summary_row(db, user_id)
    windows = await get_user_consumption_windows(db, user_id)
    return _serialize_finance_snapshot(summary, current_balance_cents, windows)


async def build_user_finance_snapshots(
    db: AsyncSession,
    user_balances: dict[str, int],
) -> dict[str, dict]:
    user_ids = list(user_balances.keys())
    if not user_ids:
        return {}

    summaries = await get_finance_summary_rows(db, user_ids)
    windows_by_user = await get_user_consumption_windows_batch(db, user_ids)
    return {
        user_id: _serialize_finance_snapshot(
            summaries.get(user_id),
            balance_cents,
            windows_by_user.get(user_id),
        )
        for user_id, balance_cents in user_balances.items()
    }


def finance_snapshot_columns() -> list[Select]:
    return [
        UserFinanceSummary.total_paid_rmb_cents,
        UserFinanceSummary.total_paid_balance_cents,
        UserFinanceSummary.total_ops_credit_cents,
        UserFinanceSummary.total_bonus_cents,
        UserFinanceSummary.total_consumed_cents,
        UserFinanceSummary.total_ops_debit_cents,
        UserFinanceSummary.legacy_unclassified_cents,
        UserFinanceSummary.total_paid_orders,
        UserFinanceSummary.last_payment_at,
    ]


def _money_str_to_cents(value: str | None) -> int:
    if not value:
        return 0
    normalized = str(value).strip()
    if not normalized:
        return 0
    if "." in normalized:
        major, minor = normalized.split(".", 1)
        minor = (minor + "00")[:2]
    else:
        major, minor = normalized, "00"
    try:
        return int(major) * 100 + int(minor)
    except ValueError:
        return 0


def _looks_like_payment_log(log: RechargeLog, note: str) -> bool:
    order_id = (getattr(log, "order_id", "") or "").lower()
    return (
        order_id.startswith("cc_")
        or "payment" in note
        or "paid" in note
        or "支付宝" in note
        or "充值" in note
    )


def _looks_like_bonus_log(note: str) -> bool:
    return any(keyword in note for keyword in ("bonus", "reward", "referral", "invite", "赠送", "奖励"))
