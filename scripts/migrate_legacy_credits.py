#!/usr/bin/env python3
"""Plan and apply the legacy-credit migration with auditable cent accounting."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


LEGACY_BALANCE = "legacy_balance"
LEGACY_TRAFFIC_PACK = "legacy_traffic_pack"
LEGACY_BALANCE_PRODUCT = "legacy_balance"
LEGACY_TRAFFIC_PACK_PRODUCT = "legacy_traffic_pack"
LEGACY_SOURCE_TYPES = {LEGACY_BALANCE, LEGACY_TRAFFIC_PACK}
KNOWN_PACK_STATUSES = {"active", "depleted", "expired", "disabled", "migrated"}
SOURCE_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
FULL_TABLE_LOCK_WARNING = (
    "Apply locks all scanned legacy User, TrafficPackBalance, and legacy CreditBalance rows "
    "in one transaction; set limits to bound the lock scope."
)


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=None)
    return value.astimezone(UTC).replace(tzinfo=None)


def _integer_cents(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be integer cents")
    return value


def _empty_totals() -> dict[str, int]:
    return {
        "observed_positive_source_cents": 0,
        "eligible_spendable_source_cents": 0,
        "planned_credit_cents": 0,
        "retained_eligible_conflict_cents": 0,
        "retained_skipped_positive_cents": 0,
        "invalid_positive_source_cents": 0,
        "retained_debt_signed_cents": 0,
        "retained_debt_abs_cents": 0,
        "observed_existing_legacy_credit_cents": 0,
        "already_migrated_credit_cents": 0,
        "before_source_cents": 0,
        "retained_source_cents": 0,
        "retired_source_cents": 0,
        "new_credit_cents": 0,
        "attempted_retired_source_cents": 0,
        "attempted_new_credit_cents": 0,
    }


def _safety(
    *,
    max_scanned_rows: int | None = None,
    max_planned_items: int | None = None,
    scanned: int = 0,
    planned: int = 0,
) -> dict[str, Any]:
    return {
        "limits": {
            "max_scanned_rows": max_scanned_rows,
            "max_planned_items": max_planned_items,
        },
        "scanned": scanned,
        "planned": planned,
        "full_table_lock_warning": FULL_TABLE_LOCK_WARNING,
    }


@dataclass(frozen=True)
class MigrationItem:
    source_type: str
    source_id: str
    source_record_id: str
    user_id: str
    product_id: str
    before_source_cents: int
    planned_credit_cents: int
    retired_source_cents: int = 0


@dataclass
class MigrationReport:
    mode: str
    as_of: datetime
    planned_items: list[MigrationItem] = field(default_factory=list)
    skips: list[dict[str, Any]] = field(default_factory=list)
    debts: list[dict[str, Any]] = field(default_factory=list)
    already_migrated: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    source_totals: dict[str, int] = field(default_factory=_empty_totals)
    zero_drift: dict[str, Any] = field(default_factory=dict)
    reconciliation: dict[str, Any] = field(
        default_factory=lambda: {"performed": False, "status": "not_applicable"}
    )
    safety: dict[str, Any] = field(default_factory=_safety)
    apply_eligible: bool = True

    @property
    def counts(self) -> dict[str, int]:
        return {
            "planned": len(self.planned_items),
            "skipped": len(self.skips),
            "debts": len(self.debts),
            "already_migrated": len(self.already_migrated),
            "conflicts": len(self.conflicts),
            "errors": len(self.errors),
        }

    def to_dict(self) -> dict[str, Any]:
        items = [asdict(item) for item in self.planned_items]
        totals = dict(self.source_totals)
        safety = dict(self.safety)
        return {
            "mode": self.mode,
            "as_of": self.as_of.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z"),
            "counts": self.counts,
            "totals": totals,
            "source_totals": dict(totals),
            "items": items,
            "planned_items": list(items),
            "skips": list(self.skips),
            "debts": list(self.debts),
            "already_migrated": list(self.already_migrated),
            "conflicts": list(self.conflicts),
            "errors": list(self.errors),
            "zero_drift": dict(self.zero_drift),
            "reconciliation": dict(self.reconciliation),
            "safety": safety,
            "limits": dict(safety["limits"]),
            "scanned": safety["scanned"],
            "planned": safety["planned"],
            "full_table_lock_warning": safety["full_table_lock_warning"],
            "apply_eligible": self.apply_eligible,
        }


@dataclass(frozen=True)
class LegacyState:
    users: list[Any]
    traffic_packs: list[Any]
    existing_credits: list[Any]


def _issue(source_type: str, source_id: str, reason: str, **details: Any) -> dict[str, Any]:
    return {
        "source_type": source_type,
        "source_id": source_id,
        "reason": reason,
        **details,
    }


def _append_safe_error(
    report: MigrationReport,
    *,
    reason: str,
    phase: str,
    exc: BaseException | None = None,
) -> None:
    details = {"phase": phase}
    if exc is not None:
        details["error_type"] = type(exc).__name__
    report.errors.append(_issue("migration", phase, reason, **details))
    report.apply_eligible = False


def _parse_source_key(source_type: str, source_id: str) -> str | None:
    if source_type not in LEGACY_SOURCE_TYPES:
        return None
    prefix = f"{source_type}:"
    if not source_id.startswith(prefix):
        return None
    record_id = source_id[len(prefix) :]
    if not record_id or not SOURCE_RECORD_ID_RE.fullmatch(record_id):
        return None
    return record_id


def _valid_credit_amount(credit: Any) -> int | None:
    original = getattr(credit, "original_cents", None)
    remaining = getattr(credit, "remaining_cents", None)
    if (
        isinstance(original, bool)
        or not isinstance(original, int)
        or original <= 0
        or isinstance(remaining, bool)
        or not isinstance(remaining, int)
        or remaining < 0
        or remaining > original
    ):
        return None
    status = str(getattr(credit, "status", "") or "")
    if status == "active" and remaining > 0:
        return original
    if status == "depleted" and remaining == 0:
        return original
    return None


def _scan_users(
    users: list[Any],
    report: MigrationReport,
    totals: dict[str, int],
) -> list[MigrationItem]:
    candidates: list[MigrationItem] = []
    for user in users:
        source_id = f"{LEGACY_BALANCE}:{user.id}"
        try:
            balance = _integer_cents(user.balance, field_name="User.balance")
        except (TypeError, ValueError) as exc:
            report.errors.append(_issue(LEGACY_BALANCE, source_id, str(exc)))
            continue
        if balance > 0:
            totals["observed_positive_source_cents"] += balance
            totals["eligible_spendable_source_cents"] += balance
            candidates.append(
                MigrationItem(
                    source_type=LEGACY_BALANCE,
                    source_id=source_id,
                    source_record_id=str(user.id),
                    user_id=str(user.id),
                    product_id=LEGACY_BALANCE_PRODUCT,
                    before_source_cents=balance,
                    planned_credit_cents=balance,
                )
            )
        elif balance < 0:
            totals["retained_debt_signed_cents"] += balance
            totals["retained_debt_abs_cents"] += abs(balance)
            report.debts.append({"user_id": str(user.id), "retained_debt_cents": balance})
        else:
            report.skips.append(
                _issue(LEGACY_BALANCE, source_id, "zero_balance")
            )
    return candidates


def _scan_packs(
    packs: list[Any],
    report: MigrationReport,
    totals: dict[str, int],
    *,
    as_of: datetime,
) -> list[MigrationItem]:
    candidates: list[MigrationItem] = []
    for pack in packs:
        source_id = f"{LEGACY_TRAFFIC_PACK}:{pack.id}"
        try:
            remaining = _integer_cents(
                pack.remaining_cents,
                field_name="TrafficPackBalance.remaining_cents",
            )
        except (TypeError, ValueError) as exc:
            report.errors.append(_issue(LEGACY_TRAFFIC_PACK, source_id, str(exc)))
            continue
        if remaining > 0:
            totals["observed_positive_source_cents"] += remaining
        try:
            original = _integer_cents(
                pack.original_cents,
                field_name="TrafficPackBalance.original_cents",
            )
        except (TypeError, ValueError) as exc:
            if remaining > 0:
                totals["invalid_positive_source_cents"] += remaining
            report.errors.append(_issue(LEGACY_TRAFFIC_PACK, source_id, str(exc)))
            continue
        if remaining < 0:
            report.errors.append(
                _issue(LEGACY_TRAFFIC_PACK, source_id, "remaining_cents_must_not_be_negative")
            )
            continue
        if original < 0 or remaining > original:
            if remaining > 0:
                totals["invalid_positive_source_cents"] += remaining
            report.errors.append(_issue(LEGACY_TRAFFIC_PACK, source_id, "invalid_pack_amount_range"))
            continue
        status = str(pack.status or "")
        if status not in KNOWN_PACK_STATUSES:
            if remaining > 0:
                totals["invalid_positive_source_cents"] += remaining
            report.errors.append(_issue(LEGACY_TRAFFIC_PACK, source_id, "unknown_pack_status"))
            continue
        if not isinstance(pack.expires_at, datetime):
            if remaining > 0:
                totals["invalid_positive_source_cents"] += remaining
            report.errors.append(_issue(LEGACY_TRAFFIC_PACK, source_id, "invalid_pack_expiry"))
            continue
        expires_at = _utc_naive(pack.expires_at)
        if status == "migrated" and remaining > 0:
            totals["invalid_positive_source_cents"] += remaining
            report.errors.append(_issue(LEGACY_TRAFFIC_PACK, source_id, "migrated_pack_not_retired"))
            continue
        if status != "active":
            if remaining > 0:
                totals["retained_skipped_positive_cents"] += remaining
            reason = "depleted" if status == "depleted" and remaining == 0 else "nonactive"
            report.skips.append(_issue(LEGACY_TRAFFIC_PACK, source_id, reason))
            continue
        if remaining == 0:
            report.skips.append(_issue(LEGACY_TRAFFIC_PACK, source_id, "depleted"))
            continue
        if expires_at <= as_of:
            totals["retained_skipped_positive_cents"] += remaining
            report.skips.append(_issue(LEGACY_TRAFFIC_PACK, source_id, "expired"))
            continue
        totals["eligible_spendable_source_cents"] += remaining
        candidates.append(
            MigrationItem(
                source_type=LEGACY_TRAFFIC_PACK,
                source_id=source_id,
                source_record_id=str(pack.id),
                user_id=str(pack.user_id),
                product_id=str(pack.product_id or LEGACY_TRAFFIC_PACK_PRODUCT),
                before_source_cents=remaining,
                planned_credit_cents=remaining,
            )
        )
    return candidates


def _index_credits(
    credits: list[Any],
    totals: dict[str, int],
) -> dict[tuple[str, str], list[Any]]:
    indexed: dict[tuple[str, str], list[Any]] = {}
    for credit in credits:
        source_type = str(getattr(credit, "source_type", "") or "")
        source_id = str(getattr(credit, "source_id", "") or "")
        if source_type not in LEGACY_SOURCE_TYPES:
            continue
        indexed.setdefault((source_type, source_id), []).append(credit)
        amount = _valid_credit_amount(credit)
        if amount is not None:
            totals["observed_existing_legacy_credit_cents"] += amount
    return indexed


def _credit_matches_candidate(credit: Any, item: MigrationItem) -> bool:
    amount = _valid_credit_amount(credit)
    return (
        amount == item.planned_credit_cents
        and str(credit.user_id) == item.user_id
        and str(credit.product_id or "") == item.product_id
    )


def _reconcile_candidates(
    candidates: list[MigrationItem],
    indexed_credits: dict[tuple[str, str], list[Any]],
    report: MigrationReport,
    totals: dict[str, int],
) -> list[MigrationItem]:
    planned: list[MigrationItem] = []
    for item in candidates:
        existing = indexed_credits.get((item.source_type, item.source_id), [])
        if not existing:
            planned.append(item)
            continue
        totals["retained_eligible_conflict_cents"] += item.before_source_cents
        if len(existing) > 1:
            reason = "duplicate_credit_source"
        elif _credit_matches_candidate(existing[0], item):
            reason = "credit_source_state_conflict"
        else:
            reason = "credit_source_terms_conflict"
        report.conflicts.append(_issue(item.source_type, item.source_id, reason))
    return planned


def _reconcile_retired_credits(
    *,
    indexed_credits: dict[tuple[str, str], list[Any]],
    candidate_keys: set[tuple[str, str]],
    users_by_id: dict[str, Any],
    packs_by_id: dict[str, Any],
    report: MigrationReport,
    totals: dict[str, int],
) -> None:
    for (source_type, source_id), existing in indexed_credits.items():
        if (source_type, source_id) in candidate_keys:
            continue
        record_id = _parse_source_key(source_type, source_id)
        if record_id is None:
            report.conflicts.append(_issue(source_type, source_id, "malformed_credit_source"))
            continue
        if len(existing) > 1:
            report.conflicts.append(_issue(source_type, source_id, "duplicate_credit_source"))
            continue
        credit = existing[0]
        amount = _valid_credit_amount(credit)
        if amount is None:
            report.conflicts.append(_issue(source_type, source_id, "credit_source_terms_conflict"))
            continue

        if source_type == LEGACY_BALANCE:
            source = users_by_id.get(record_id)
            if source is None:
                report.conflicts.append(_issue(source_type, source_id, "orphan_credit_source"))
                continue
            terms_match = (
                str(credit.user_id) == record_id
                and str(credit.product_id or "") == LEGACY_BALANCE_PRODUCT
            )
            source_retired = getattr(source, "balance", None) == 0
        else:
            source = packs_by_id.get(record_id)
            if source is None:
                report.conflicts.append(_issue(source_type, source_id, "orphan_credit_source"))
                continue
            original = getattr(source, "original_cents", None)
            terms_match = (
                str(credit.user_id) == str(source.user_id)
                and str(credit.product_id or "")
                == str(source.product_id or LEGACY_TRAFFIC_PACK_PRODUCT)
                and isinstance(original, int)
                and not isinstance(original, bool)
                and 0 < amount <= original
            )
            source_retired = (
                getattr(source, "remaining_cents", None) == 0
                and str(getattr(source, "status", "") or "") == "migrated"
            )

        if not terms_match:
            report.conflicts.append(_issue(source_type, source_id, "credit_source_terms_conflict"))
        elif not source_retired:
            report.conflicts.append(_issue(source_type, source_id, "credit_source_state_conflict"))
        else:
            report.already_migrated.append(
                {
                    "source_type": source_type,
                    "source_id": source_id,
                    "credit_balance_id": str(credit.id),
                    "amount_cents": amount,
                }
            )
            totals["already_migrated_credit_cents"] += amount


def _finalize_plan(report: MigrationReport, totals: dict[str, int]) -> MigrationReport:
    totals["planned_credit_cents"] = sum(
        item.planned_credit_cents for item in report.planned_items
    )
    totals["before_source_cents"] = totals["observed_positive_source_cents"]
    totals["retained_source_cents"] = (
        totals["retained_eligible_conflict_cents"]
        + totals["retained_skipped_positive_cents"]
        + totals["invalid_positive_source_cents"]
    )
    source_difference = (
        totals["observed_positive_source_cents"]
        - totals["eligible_spendable_source_cents"]
        - totals["retained_skipped_positive_cents"]
        - totals["invalid_positive_source_cents"]
    )
    migration_difference = (
        totals["eligible_spendable_source_cents"]
        - totals["planned_credit_cents"]
        - totals["retained_eligible_conflict_cents"]
    )
    report.source_totals = totals
    report.zero_drift = {
        "source_coverage_equation": (
            "observed_positive_source_cents = eligible_spendable_source_cents + "
            "retained_skipped_positive_cents + invalid_positive_source_cents"
        ),
        "migration_coverage_equation": (
            "eligible_spendable_source_cents = planned_credit_cents + "
            "retained_eligible_conflict_cents"
        ),
        "source_coverage_difference_cents": source_difference,
        "migration_coverage_difference_cents": migration_difference,
        "difference_cents": source_difference + migration_difference,
        "conserved": source_difference == 0 and migration_difference == 0,
    }
    report.apply_eligible = (
        not report.conflicts and not report.errors and report.zero_drift["conserved"]
    )
    return report


def _set_safety(
    report: MigrationReport,
    *,
    max_scanned_rows: int | None,
    max_planned_items: int | None,
    scanned: int | None = None,
) -> None:
    report.safety = _safety(
        max_scanned_rows=max_scanned_rows,
        max_planned_items=max_planned_items,
        scanned=report.safety.get("scanned", 0) if scanned is None else scanned,
        planned=len(report.planned_items),
    )


def _refuse_apply_limit(report: MigrationReport, *, reason: str) -> MigrationReport:
    report.mode = "apply_refused"
    _append_safe_error(report, reason=reason, phase="apply_limits")
    return report


def build_migration_plan(
    users: Iterable[Any],
    traffic_packs: Iterable[Any],
    existing_credits: Iterable[Any],
    *,
    as_of: datetime,
) -> MigrationReport:
    current = _utc_naive(as_of)
    report = MigrationReport(mode="dry_run", as_of=current)
    totals = _empty_totals()
    user_rows = sorted(users, key=lambda item: str(item.id))
    pack_rows = sorted(traffic_packs, key=lambda item: (str(item.user_id), str(item.id)))
    credit_rows = sorted(
        existing_credits,
        key=lambda item: (
            str(getattr(item, "source_type", "") or ""),
            str(getattr(item, "source_id", "") or ""),
            str(getattr(item, "id", "") or ""),
        ),
    )

    candidates = _scan_users(user_rows, report, totals)
    candidates.extend(_scan_packs(pack_rows, report, totals, as_of=current))
    indexed_credits = _index_credits(credit_rows, totals)
    candidate_keys = {(item.source_type, item.source_id) for item in candidates}
    report.planned_items = _reconcile_candidates(
        candidates,
        indexed_credits,
        report,
        totals,
    )
    _reconcile_retired_credits(
        indexed_credits=indexed_credits,
        candidate_keys=candidate_keys,
        users_by_id={str(item.id): item for item in user_rows},
        packs_by_id={str(item.id): item for item in pack_rows},
        report=report,
        totals=totals,
    )
    for pack in pack_rows:
        if (
            str(getattr(pack, "status", "") or "") == "migrated"
            and getattr(pack, "remaining_cents", None) == 0
            and (LEGACY_TRAFFIC_PACK, f"{LEGACY_TRAFFIC_PACK}:{pack.id}")
            not in indexed_credits
        ):
            report.conflicts.append(
                _issue(
                    LEGACY_TRAFFIC_PACK,
                    f"{LEGACY_TRAFFIC_PACK}:{pack.id}",
                    "missing_migration_batch",
                )
            )

    report.planned_items.sort(
        key=lambda item: (item.user_id, item.source_type, item.source_record_id)
    )
    report.skips.sort(key=lambda item: (item["source_type"], item["source_id"], item["reason"]))
    report.debts.sort(key=lambda item: item["user_id"])
    report.already_migrated.sort(
        key=lambda item: (item["source_type"], item["source_id"])
    )
    report.conflicts.sort(
        key=lambda item: (item["source_type"], item["source_id"], item["reason"])
    )
    report.errors.sort(
        key=lambda item: (item["source_type"], item["source_id"], item["reason"])
    )
    _set_safety(
        report,
        max_scanned_rows=None,
        max_planned_items=None,
        scanned=len(user_rows) + len(pack_rows) + len(credit_rows),
    )
    return _finalize_plan(report, totals)


def _result_rows(result: Any) -> list[Any]:
    if result is None:
        return []
    scalars = result.scalars() if hasattr(result, "scalars") else result
    return list(scalars.all() or [])


async def load_legacy_state(db: Any, *, for_update: bool = False) -> LegacyState:
    """Load all migration-owned rows without flushing unrelated pending ORM state."""
    from sqlalchemy import select

    from app.models import CreditBalance, TrafficPackBalance, User

    users_query = select(User).order_by(User.id.asc())
    packs_query = select(TrafficPackBalance).order_by(
        TrafficPackBalance.user_id.asc(), TrafficPackBalance.id.asc()
    )
    credits_query = (
        select(CreditBalance)
        .where(CreditBalance.source_type.in_([LEGACY_BALANCE, LEGACY_TRAFFIC_PACK]))
        .order_by(
            CreditBalance.source_type.asc(),
            CreditBalance.source_id.asc(),
            CreditBalance.id.asc(),
        )
    )
    if for_update:
        users_query = users_query.execution_options(populate_existing=True).with_for_update()
        packs_query = packs_query.execution_options(populate_existing=True).with_for_update()
        credits_query = credits_query.execution_options(populate_existing=True).with_for_update()
    with db.no_autoflush:
        users = _result_rows(await db.execute(users_query))
        packs = _result_rows(await db.execute(packs_query))
        credits = _result_rows(await db.execute(credits_query))
    return LegacyState(users=users, traffic_packs=packs, existing_credits=credits)


def _build_from_state(state: LegacyState, *, as_of: datetime) -> MigrationReport:
    return build_migration_plan(
        state.users,
        state.traffic_packs,
        state.existing_credits,
        as_of=as_of,
    )


def _session_has_pending_changes(db: Any) -> bool:
    for attribute in ("new", "dirty", "deleted"):
        pending = getattr(db, attribute, None)
        if pending is not None and len(pending) > 0:
            return True
    return False


def _plan_fingerprint(report: MigrationReport) -> dict[str, Any]:
    return {
        "planned_items": [asdict(item) for item in report.planned_items],
        "skips": report.skips,
        "debts": report.debts,
        "already_migrated": report.already_migrated,
        "conflicts": report.conflicts,
        "errors": report.errors,
        "totals": report.source_totals,
        "zero_drift": report.zero_drift,
        "apply_eligible": report.apply_eligible,
    }


def _validate_grant(grant: Any, item: MigrationItem) -> int:
    amount = _valid_credit_amount(grant)
    if (
        amount != item.planned_credit_cents
        or str(getattr(grant, "user_id", "")) != item.user_id
        or str(getattr(grant, "source_type", "")) != item.source_type
        or str(getattr(grant, "source_id", "")) != item.source_id
        or str(getattr(grant, "product_id", "") or "") != item.product_id
    ):
        raise RuntimeError("grant result terms mismatch")
    return amount


def _mark_transfer(
    report: MigrationReport,
    *,
    retired_cents: int,
    new_credit_cents: int,
    committed: bool,
) -> None:
    report.source_totals["attempted_retired_source_cents"] = retired_cents
    report.source_totals["attempted_new_credit_cents"] = new_credit_cents
    if committed:
        report.source_totals["retired_source_cents"] = retired_cents
        report.source_totals["new_credit_cents"] = new_credit_cents
        report.planned_items = [
            replace(item, retired_source_cents=item.before_source_cents)
            for item in report.planned_items
        ]
    else:
        report.source_totals["retired_source_cents"] = 0
        report.source_totals["new_credit_cents"] = 0
    difference = retired_cents - new_credit_cents
    report.zero_drift.update(
        {
            "apply_transfer_equation": (
                "old_spendable_decrease_cents = new_credit_increase_cents"
            ),
            "old_spendable_decrease_cents": retired_cents,
            "new_credit_increase_cents": new_credit_cents,
            "apply_transfer_difference_cents": difference,
            "total_spendable_delta_cents": new_credit_cents - retired_cents,
            "apply_transfer_conserved": difference == 0,
        }
    )


async def _rollback_error(db: Any) -> BaseException | None:
    try:
        await db.rollback()
    except BaseException as exc:
        return exc
    return None


def _precommit_failure(
    report: MigrationReport,
    *,
    reason: str,
    phase: str,
    exc: BaseException,
    rollback_exc: BaseException | None,
    retired_cents: int,
    new_credit_cents: int,
) -> MigrationReport:
    _mark_transfer(
        report,
        retired_cents=retired_cents,
        new_credit_cents=new_credit_cents,
        committed=False,
    )
    report.mode = "apply_failed" if rollback_exc is None else "apply_indeterminate"
    _append_safe_error(report, reason=reason, phase=phase, exc=exc)
    if rollback_exc is not None:
        _append_safe_error(
            report,
            reason="rollback_failed",
            phase="rollback",
            exc=rollback_exc,
        )
    return report


def _post_commit_verified(
    post_report: MigrationReport,
    applied_items: list[MigrationItem],
) -> bool:
    if post_report.conflicts or post_report.errors or not post_report.apply_eligible:
        return False
    migrated = {
        (item["source_type"], item["source_id"], item["amount_cents"])
        for item in post_report.already_migrated
    }
    expected = {
        (item.source_type, item.source_id, item.planned_credit_cents)
        for item in applied_items
    }
    return expected.issubset(migrated)


async def migrate_legacy_credits(
    db: Any,
    *,
    as_of: datetime,
    apply: bool = False,
    max_scanned_rows: int | None = None,
    max_planned_items: int | None = None,
) -> MigrationReport:
    """Dry-run by default; explicit apply is all-or-nothing before commit."""
    current = _utc_naive(as_of)
    if _session_has_pending_changes(db):
        report = _finalize_plan(
            MigrationReport(
                mode="apply_refused" if apply else "dry_run_refused",
                as_of=current,
            ),
            _empty_totals(),
        )
        report.mode = "apply_refused" if apply else "dry_run_refused"
        _set_safety(
            report,
            max_scanned_rows=max_scanned_rows,
            max_planned_items=max_planned_items,
        )
        _append_safe_error(
            report,
            reason="session_has_pending_changes",
            phase="session_preflight",
        )
        return report
    try:
        initial_state = await load_legacy_state(db, for_update=False)
    except BaseException as exc:
        report = _finalize_plan(MigrationReport(mode="dry_run_failed", as_of=current), _empty_totals())
        report.mode = "apply_failed" if apply else "dry_run_failed"
        _set_safety(
            report,
            max_scanned_rows=max_scanned_rows,
            max_planned_items=max_planned_items,
        )
        _append_safe_error(
            report,
            reason="database_operation_failed",
            phase="initial_read",
            exc=exc,
        )
        if apply:
            rollback_exc = await _rollback_error(db)
            if rollback_exc is not None:
                report.mode = "apply_indeterminate"
                _append_safe_error(
                    report,
                    reason="rollback_failed",
                    phase="rollback",
                    exc=rollback_exc,
                )
        return report

    initial_report = _build_from_state(initial_state, as_of=current)
    _set_safety(
        initial_report,
        max_scanned_rows=max_scanned_rows,
        max_planned_items=max_planned_items,
    )
    if not apply:
        return initial_report
    if max_scanned_rows is None or max_planned_items is None:
        return _refuse_apply_limit(initial_report, reason="apply_limits_required")
    if (
        isinstance(max_scanned_rows, bool)
        or not isinstance(max_scanned_rows, int)
        or max_scanned_rows <= 0
        or isinstance(max_planned_items, bool)
        or not isinstance(max_planned_items, int)
        or max_planned_items <= 0
    ):
        return _refuse_apply_limit(initial_report, reason="apply_limits_invalid")
    if (
        initial_report.safety["scanned"] > max_scanned_rows
        or initial_report.safety["planned"] > max_planned_items
    ):
        return _refuse_apply_limit(initial_report, reason="apply_limit_exceeded")
    if not initial_report.apply_eligible:
        initial_report.mode = "apply_refused"
        return initial_report
    try:
        locked_state = await load_legacy_state(db, for_update=True)
    except BaseException as exc:
        rollback_exc = await _rollback_error(db)
        return _precommit_failure(
            initial_report,
            reason="locked_recheck_failed",
            phase="locked_read",
            exc=exc,
            rollback_exc=rollback_exc,
            retired_cents=0,
            new_credit_cents=0,
        )

    locked_report = _build_from_state(locked_state, as_of=current)
    _set_safety(
        locked_report,
        max_scanned_rows=max_scanned_rows,
        max_planned_items=max_planned_items,
    )
    if _plan_fingerprint(locked_report) != _plan_fingerprint(initial_report):
        locked_report.mode = "apply_refused"
        locked_report.conflicts.append(
            _issue("migration", "locked_recheck", "source_changed_after_plan")
        )
        locked_report.apply_eligible = False
        rollback_exc = await _rollback_error(db)
        if rollback_exc is not None:
            locked_report.mode = "apply_indeterminate"
            _append_safe_error(
                locked_report,
                reason="rollback_failed",
                phase="rollback",
                exc=rollback_exc,
            )
        return locked_report

    if not locked_report.planned_items:
        rollback_exc = await _rollback_error(db)
        if rollback_exc is not None:
            locked_report.mode = "apply_indeterminate"
            _append_safe_error(
                locked_report,
                reason="rollback_failed",
                phase="verified_noop_rollback",
                exc=rollback_exc,
            )
            return locked_report
        locked_report.mode = "apply"
        locked_report.reconciliation = {"performed": True, "status": "verified_noop"}
        _mark_transfer(
            locked_report,
            retired_cents=0,
            new_credit_cents=0,
            committed=True,
        )
        return locked_report

    from app.credit_wallet import grant_permanent_credit

    users_by_id = {str(item.id): item for item in locked_state.users}
    packs_by_id = {str(item.id): item for item in locked_state.traffic_packs}
    retired_cents = 0
    new_credit_cents = 0
    try:
        for item in locked_report.planned_items:
            grant = await grant_permanent_credit(
                db,
                user_id=item.user_id,
                source_type=item.source_type,
                source_id=item.source_id,
                amount_cents=item.planned_credit_cents,
                product_id=item.product_id,
            )
            new_credit_cents += _validate_grant(grant, item)
            retired_cents += item.before_source_cents
            if item.source_type == LEGACY_BALANCE:
                users_by_id[item.source_record_id].balance = 0
            else:
                pack = packs_by_id[item.source_record_id]
                pack.remaining_cents = 0
                pack.status = "migrated"
    except BaseException as exc:
        rollback_exc = await _rollback_error(db)
        return _precommit_failure(
            locked_report,
            reason="grant_failed",
            phase="grant",
            exc=exc,
            rollback_exc=rollback_exc,
            retired_cents=retired_cents,
            new_credit_cents=new_credit_cents,
        )

    if retired_cents != new_credit_cents:
        exc = RuntimeError("transfer accounting mismatch")
        rollback_exc = await _rollback_error(db)
        return _precommit_failure(
            locked_report,
            reason="transfer_accounting_mismatch",
            phase="precommit_accounting",
            exc=exc,
            rollback_exc=rollback_exc,
            retired_cents=retired_cents,
            new_credit_cents=new_credit_cents,
        )

    try:
        await db.flush()
    except BaseException as exc:
        rollback_exc = await _rollback_error(db)
        return _precommit_failure(
            locked_report,
            reason="flush_failed",
            phase="flush",
            exc=exc,
            rollback_exc=rollback_exc,
            retired_cents=retired_cents,
            new_credit_cents=new_credit_cents,
        )

    try:
        await db.commit()
    except BaseException as exc:
        rollback_exc = await _rollback_error(db)
        _mark_transfer(
            locked_report,
            retired_cents=retired_cents,
            new_credit_cents=new_credit_cents,
            committed=False,
        )
        locked_report.mode = "apply_indeterminate"
        _append_safe_error(
            locked_report,
            reason="commit_outcome_unknown",
            phase="commit",
            exc=exc,
        )
        if rollback_exc is not None:
            _append_safe_error(
                locked_report,
                reason="rollback_failed",
                phase="rollback",
                exc=rollback_exc,
            )
        return locked_report

    locked_report.mode = "apply"
    _mark_transfer(
        locked_report,
        retired_cents=retired_cents,
        new_credit_cents=new_credit_cents,
        committed=True,
    )
    try:
        db.expire_all()
        post_state = await load_legacy_state(db, for_update=False)
        post_report = _build_from_state(post_state, as_of=current)
    except BaseException as exc:
        locked_report.mode = "apply_indeterminate"
        locked_report.reconciliation = {"performed": True, "status": "read_failed"}
        _append_safe_error(
            locked_report,
            reason="reconciliation_read_failed",
            phase="post_commit_reconciliation",
            exc=exc,
        )
        return locked_report

    if not _post_commit_verified(post_report, locked_report.planned_items):
        locked_report.mode = "apply_indeterminate"
        locked_report.reconciliation = {
            "performed": True,
            "status": "failed",
            "post_conflicts": len(post_report.conflicts),
            "post_errors": len(post_report.errors),
        }
        _append_safe_error(
            locked_report,
            reason="reconciliation_failed",
            phase="post_commit_reconciliation",
        )
        return locked_report

    locked_report.reconciliation = {"performed": True, "status": "verified"}
    return locked_report


def _parse_as_of(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("--as-of must be an ISO-8601 datetime") from exc
    return _utc_naive(parsed)


def _positive_limit(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("limit must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("limit must be a positive integer")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate positive legacy balances and valid traffic packs to permanent credits. "
            "Defaults to dry-run; only --apply writes. Apply locks all scanned legacy rows "
            "in one transaction and therefore requires explicit safety limits."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Apply transactionally. Requires --max-scanned-rows and --max-planned-items; "
            "without --apply the command is dry-run only."
        ),
    )
    parser.add_argument(
        "--max-scanned-rows",
        type=_positive_limit,
        help="Maximum total User, TrafficPackBalance, and legacy CreditBalance rows scanned.",
    )
    parser.add_argument(
        "--max-planned-items",
        type=_positive_limit,
        help="Maximum migration items allowed in the plan.",
    )
    parser.add_argument(
        "--as-of",
        type=_parse_as_of,
        help="UTC/ISO-8601 cutoff for traffic-pack validity (default: current UTC time).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print only machine-readable JSON (default also prints a human summary to stderr).",
    )
    return parser.parse_args(argv)


def _human_items(name: str, items: list[dict[str, Any]]) -> list[str]:
    lines = [f"{name}:"]
    if not items:
        lines.append("  (none)")
    else:
        lines.extend(
            f"  - {json.dumps(item, ensure_ascii=False, sort_keys=True)}" for item in items
        )
    return lines


def render_human_summary(report: MigrationReport) -> str:
    payload = report.to_dict()
    counts = payload["counts"]
    totals = payload["totals"]
    lines = [
        f"mode: {report.mode}",
        f"as_of: {payload['as_of']}",
        (
            "counts: "
            f"planned: {counts['planned']}, skipped: {counts['skipped']}, "
            f"debts: {counts['debts']}, already_migrated: {counts['already_migrated']}, "
            f"conflicts: {counts['conflicts']}, errors: {counts['errors']}"
        ),
        "totals: " + ", ".join(f"{key}: {value}" for key, value in totals.items()),
        f"zero_drift: {'yes' if report.zero_drift.get('conserved') else 'no'}",
        f"reconciliation: {report.reconciliation.get('status', 'unknown')}",
        f"safety: {json.dumps(payload['safety'], ensure_ascii=False, sort_keys=True)}",
        f"full_table_lock_warning: {payload['full_table_lock_warning']}",
        f"apply_eligible: {'yes' if report.apply_eligible else 'no'}",
    ]
    for name in (
        "planned_items",
        "skips",
        "debts",
        "already_migrated",
        "conflicts",
        "errors",
    ):
        lines.extend(_human_items(name, payload[name]))
    return "\n".join(lines)


async def _run_cli(args: argparse.Namespace) -> MigrationReport:
    project_root = str(Path(__file__).resolve().parents[1])
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    from app.db import SessionLocal

    as_of = args.as_of or _utc_naive(datetime.now(UTC))
    async with SessionLocal() as db:
        return await migrate_legacy_credits(
            db,
            as_of=as_of,
            apply=args.apply,
            max_scanned_rows=args.max_scanned_rows,
            max_planned_items=args.max_planned_items,
        )


def _unexpected_cli_failure(
    *,
    as_of: datetime,
    apply: bool,
    exc: BaseException,
) -> MigrationReport:
    report = _finalize_plan(
        MigrationReport(
            mode="apply_failed" if apply else "dry_run_failed",
            as_of=as_of,
        ),
        _empty_totals(),
    )
    report.mode = "apply_failed" if apply else "dry_run_failed"
    _append_safe_error(
        report,
        reason="database_operation_failed",
        phase="cli",
        exc=exc,
    )
    return report


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    as_of = args.as_of or _utc_naive(datetime.now(UTC))
    if args.apply and (
        args.max_scanned_rows is None or args.max_planned_items is None
    ):
        report = _finalize_plan(
            MigrationReport(mode="apply_refused", as_of=as_of),
            _empty_totals(),
        )
        _set_safety(
            report,
            max_scanned_rows=args.max_scanned_rows,
            max_planned_items=args.max_planned_items,
        )
        _refuse_apply_limit(report, reason="apply_limits_required")
    else:
        try:
            report = asyncio.run(_run_cli(args))
        except BaseException as exc:
            report = _unexpected_cli_failure(as_of=as_of, apply=args.apply, exc=exc)
    if not args.json:
        print(render_human_summary(report), file=sys.stderr)
    print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
    successful_mode = report.mode in {"dry_run", "apply"}
    return 0 if successful_mode and report.apply_eligible else 2


if __name__ == "__main__":
    raise SystemExit(main())
