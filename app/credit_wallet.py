from __future__ import annotations

from datetime import UTC, datetime
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import CreditAllocation, CreditBalance
from .security import generate_id


class CreditWalletError(Exception):
    pass


class InsufficientCreditError(CreditWalletError):
    def __init__(self, *, available_cents: int, required_cents: int):
        super().__init__("insufficient permanent credit")
        self.available_cents = available_cents
        self.required_cents = required_cents


class CreditSourceConflictError(CreditWalletError):
    pass


def _positive_cents(value: int, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be integer cents")
    if value <= 0:
        raise ValueError(f"{field} must be positive")
    return value


def _required_text(value: str, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    return normalized


def _result_rows(result) -> list:
    if result is None:
        return []
    if hasattr(result, "scalars"):
        scalars = result.scalars()
        if hasattr(scalars, "all"):
            return list(scalars.all() or [])
    if hasattr(result, "all"):
        return list(result.all() or [])
    scalar_one_or_none = getattr(result, "scalar_one_or_none", None)
    if callable(scalar_one_or_none):
        value = scalar_one_or_none()
        return [] if value is None else [value]
    scalar = getattr(result, "scalar", None)
    if callable(scalar):
        value = scalar()
        return [] if value is None else [value]
    return []


def _result_one(result):
    if result is None:
        return None
    scalar_one_or_none = getattr(result, "scalar_one_or_none", None)
    if callable(scalar_one_or_none):
        return scalar_one_or_none()
    rows = _result_rows(result)
    return rows[0] if rows else None


def _ordered_credit_balances(query):
    return query.order_by(CreditBalance.created_at.asc(), CreditBalance.id.asc())


def _utc_naive(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        return current.replace(tzinfo=None)
    return current.astimezone(UTC).replace(tzinfo=None)


async def _credit_by_source(
    db: AsyncSession,
    *,
    source_type: str,
    source_id: str,
    for_update: bool = False,
) -> CreditBalance | None:
    query = select(CreditBalance).where(
        CreditBalance.source_type == source_type,
        CreditBalance.source_id == source_id,
    )
    if for_update:
        query = _ordered_credit_balances(query).with_for_update()
    return _result_one(await db.execute(query))


def _validate_source_terms(
    balance: CreditBalance,
    *,
    user_id: str,
    amount_cents: int,
    product_id: str,
) -> None:
    if (
        balance.user_id != user_id
        or int(balance.original_cents or 0) != amount_cents
        or str(balance.product_id or "") != product_id
    ):
        raise CreditSourceConflictError("credit source already granted with different terms")


def _is_source_unique_conflict(exc: IntegrityError) -> bool:
    message = f"{exc} {getattr(exc, 'orig', '')}".lower()
    is_duplicate = "duplicate" in message or "unique constraint failed" in message
    identifies_source = (
        "uq_credit_balances_source" in message
        or (
            "coincoin_credit_balances.source_type" in message
            and "coincoin_credit_balances.source_id" in message
        )
    )
    return is_duplicate and identifies_source


async def grant_permanent_credit(
    db: AsyncSession,
    *,
    user_id: str,
    source_type: str,
    source_id: str,
    amount_cents: int,
    product_id: str = "",
) -> CreditBalance:
    """Grant one non-expiring batch, returning the existing batch on source replay."""
    amount = _positive_cents(amount_cents, field="amount_cents")
    normalized_user_id = _required_text(user_id, field="user_id")
    normalized_source_type = _required_text(source_type, field="source_type")
    normalized_source_id = _required_text(source_id, field="source_id")
    normalized_product_id = str(product_id or "")
    existing = await _credit_by_source(
        db,
        source_type=normalized_source_type,
        source_id=normalized_source_id,
    )
    if existing is not None:
        _validate_source_terms(
            existing,
            user_id=normalized_user_id,
            amount_cents=amount,
            product_id=normalized_product_id,
        )
        return existing

    balance = CreditBalance(
        id=generate_id("cb_"),
        user_id=normalized_user_id,
        source_type=normalized_source_type,
        source_id=normalized_source_id,
        product_id=normalized_product_id,
        status="active",
        original_cents=amount,
        remaining_cents=amount,
    )
    try:
        async with db.begin_nested():
            db.add(balance)
            await db.flush()
    except IntegrityError as exc:
        if not _is_source_unique_conflict(exc):
            raise
        winner = await _credit_by_source(
            db,
            source_type=normalized_source_type,
            source_id=normalized_source_id,
            for_update=True,
        )
        if winner is None:
            raise CreditWalletError("credit source conflict winner not found") from exc
        _validate_source_terms(
            winner,
            user_id=normalized_user_id,
            amount_cents=amount,
            product_id=normalized_product_id,
        )
        return winner
    return balance


async def list_spendable_credit_batches(
    db: AsyncSession,
    user_id: str,
    *,
    for_update: bool = False,
) -> list[CreditBalance]:
    query = _ordered_credit_balances(
        select(CreditBalance)
        .where(
            CreditBalance.user_id == _required_text(user_id, field="user_id"),
            CreditBalance.status == "active",
            CreditBalance.remaining_cents > 0,
        )
    )
    if for_update:
        query = query.with_for_update()
    return _result_rows(await db.execute(query))


async def total_spendable_credit_cents(db: AsyncSession, user_id: str) -> int:
    batches = await list_spendable_credit_batches(db, user_id)
    return sum(max(0, int(batch.remaining_cents or 0)) for batch in batches)


async def debit_credit_batches(
    db: AsyncSession,
    *,
    user_id: str,
    amount_cents: int,
) -> dict:
    """Debit stable FIFO batches after locking and prechecking the full amount."""
    amount = _positive_cents(amount_cents, field="amount_cents")
    normalized_user_id = _required_text(user_id, field="user_id")
    batches = await list_spendable_credit_batches(db, normalized_user_id, for_update=True)
    available = sum(max(0, int(batch.remaining_cents or 0)) for batch in batches)
    if available < amount:
        raise InsufficientCreditError(available_cents=available, required_cents=amount)

    remaining = amount
    payload = []
    for batch in batches:
        if remaining <= 0:
            break
        allocated = min(remaining, max(0, int(batch.remaining_cents or 0)))
        if allocated <= 0:
            continue
        allocation = CreditAllocation(
            id=generate_id("ca_"),
            user_id=normalized_user_id,
            credit_balance_id=batch.id,
            amount_cents=allocated,
        )
        batch.remaining_cents = int(batch.remaining_cents or 0) - allocated
        batch.status = "active" if batch.remaining_cents > 0 else "depleted"
        db.add(allocation)
        payload.append(
            {
                "allocation_id": allocation.id,
                "credit_balance_id": batch.id,
                "amount_cents": allocated,
            }
        )
        remaining -= allocated

    return {"debited_cents": amount, "allocations": payload}


async def refund_credit_allocations(
    db: AsyncSession,
    *,
    user_id: str,
    allocation_ids: Iterable[str],
    expected_allocations: Iterable[dict] | None = None,
    now: datetime | None = None,
) -> dict:
    """Refund allocations; optional expected metadata enables strict fail-closed mode."""
    normalized_user_id = _required_text(user_id, field="user_id")
    normalized_ids = sorted({_required_text(value, field="allocation_id") for value in allocation_ids})
    if not normalized_ids:
        return {"refunded_cents": 0, "allocations": []}
    expected_by_id = None
    if expected_allocations is not None:
        expected_by_id = {}
        try:
            for item in expected_allocations:
                allocation_id = _required_text(item.get("allocation_id"), field="allocation_id")
                if allocation_id in expected_by_id:
                    raise CreditWalletError("duplicate expected credit allocation")
                expected_by_id[allocation_id] = {
                    "allocation_id": allocation_id,
                    "credit_balance_id": _required_text(
                        item.get("credit_balance_id"),
                        field="credit_balance_id",
                    ),
                    "amount_cents": _positive_cents(
                        item.get("amount_cents"),
                        field="amount_cents",
                    ),
                }
        except (AttributeError, TypeError, ValueError) as exc:
            raise CreditWalletError("invalid expected credit allocation metadata") from exc
        if set(expected_by_id) != set(normalized_ids):
            raise CreditWalletError("expected credit allocation ids do not match")

    allocation_query = (
        select(CreditAllocation)
        .where(
            CreditAllocation.user_id == normalized_user_id,
            CreditAllocation.id.in_(normalized_ids),
        )
        .order_by(CreditAllocation.created_at.asc(), CreditAllocation.id.asc())
        .with_for_update()
    )
    allocations = _result_rows(await db.execute(allocation_query))
    if {allocation.id for allocation in allocations} != set(normalized_ids):
        raise CreditWalletError("credit allocation not found")

    pending = [allocation for allocation in allocations if allocation.refunded_at is None]
    if expected_by_id is None and not pending:
        return {"refunded_cents": 0, "allocations": []}

    refund_candidates = allocations if expected_by_id is not None else pending
    balance_ids = sorted({allocation.credit_balance_id for allocation in refund_candidates})
    balance_query = _ordered_credit_balances(
        select(CreditBalance)
        .where(
            CreditBalance.user_id == normalized_user_id,
            CreditBalance.id.in_(balance_ids),
        )
    ).with_for_update()
    balances = _result_rows(await db.execute(balance_query))
    balances_by_id = {balance.id: balance for balance in balances}
    if set(balances_by_id) != set(balance_ids):
        raise CreditWalletError("credit balance for allocation not found")

    if expected_by_id is not None:
        for allocation in allocations:
            expected = expected_by_id[allocation.id]
            if (
                str(allocation.user_id or "") != normalized_user_id
                or allocation.refunded_at is not None
                or str(allocation.credit_balance_id or "") != expected["credit_balance_id"]
                or int(allocation.amount_cents or 0) != expected["amount_cents"]
            ):
                raise CreditWalletError("credit allocation metadata mismatch")
        if any(str(balance.user_id or "") != normalized_user_id for balance in balances):
            raise CreditWalletError("credit balance owner mismatch")

    restore_by_balance: dict[str, int] = {}
    for allocation in refund_candidates:
        restore_by_balance[allocation.credit_balance_id] = (
            restore_by_balance.get(allocation.credit_balance_id, 0)
            + int(allocation.amount_cents or 0)
        )
    for balance_id, restore_cents in restore_by_balance.items():
        balance = balances_by_id[balance_id]
        restored_total = int(balance.remaining_cents or 0) + restore_cents
        if restored_total > int(balance.original_cents or 0):
            raise CreditWalletError("credit refund would exceed original batch amount")

    refunded_at = _utc_naive(now)
    payload = []
    for balance_id, restore_cents in restore_by_balance.items():
        balance = balances_by_id[balance_id]
        balance.remaining_cents = int(balance.remaining_cents or 0) + restore_cents
        balance.status = "active"
    for allocation in refund_candidates:
        allocation.refunded_at = refunded_at
        payload.append(
            {
                "allocation_id": allocation.id,
                "credit_balance_id": allocation.credit_balance_id,
                "amount_cents": int(allocation.amount_cents or 0),
            }
        )

    return {
        "refunded_cents": sum(item["amount_cents"] for item in payload),
        "allocations": payload,
    }
