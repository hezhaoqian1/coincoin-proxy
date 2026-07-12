import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from scripts import migrate_legacy_credits as migration


AS_OF = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
APPLY_LIMITS = {"max_scanned_rows": 1000, "max_planned_items": 1000}


async def _apply(db):
    return await migration.migrate_legacy_credits(
        db,
        as_of=AS_OF,
        apply=True,
        **APPLY_LIMITS,
    )


def _user(user_id: str, balance: int):
    return SimpleNamespace(id=user_id, balance=balance)


def _pack(
    pack_id: str,
    *,
    user_id: str = "u_1",
    product_id: str = "traffic_10",
    status: str = "active",
    original_cents: int = 1000,
    remaining_cents: int = 600,
    expires_at: datetime | None = None,
):
    return SimpleNamespace(
        id=pack_id,
        user_id=user_id,
        product_id=product_id,
        status=status,
        original_cents=original_cents,
        remaining_cents=remaining_cents,
        expires_at=expires_at or AS_OF + timedelta(days=1),
    )


def _credit(
    credit_id: str,
    *,
    user_id: str,
    source_type: str,
    source_id: str,
    product_id: str,
    original_cents: int,
    remaining_cents: int | None = None,
):
    return SimpleNamespace(
        id=credit_id,
        user_id=user_id,
        source_type=source_type,
        source_id=source_id,
        product_id=product_id,
        status="active",
        original_cents=original_cents,
        remaining_cents=original_cents if remaining_cents is None else remaining_cents,
        created_at=AS_OF,
    )


class _EntityResult:
    def __init__(self, values):
        self.values = list(values)

    def scalars(self):
        return self

    def all(self):
        return list(self.values)

    def scalar_one_or_none(self):
        if len(self.values) > 1:
            raise AssertionError("expected at most one row")
        return self.values[0] if self.values else None


class _FakeSession:
    def __init__(
        self,
        *,
        users=(),
        packs=(),
        credits=(),
        change_on_lock=None,
        fail_flush_at=None,
        fail_commit=False,
        fail_rollback=False,
        fail_post_commit_read=False,
        after_commit_mutation=None,
    ):
        self.users = list(users)
        self.packs = list(packs)
        self.credits = list(credits)
        self.change_on_lock = change_on_lock
        self.fail_flush_at = fail_flush_at
        self.fail_commit = fail_commit
        self.fail_rollback = fail_rollback
        self.fail_post_commit_read = fail_post_commit_read
        self.after_commit_mutation = after_commit_mutation
        self.queries = []
        self.added = []
        self.flushes = 0
        self.commits = 0
        self.rollbacks = 0
        self.nested_begins = 0
        self.no_autoflush_enters = 0
        self.no_autoflush_exits = 0
        self.expire_all_calls = 0
        self.commit_completed = False
        self._lock_change_applied = False
        self._snapshot = self._take_snapshot()

    def _take_snapshot(self):
        return {
            "users": [(item, item.balance) for item in self.users],
            "packs": [(item, item.remaining_cents, item.status) for item in self.packs],
            "credits": list(self.credits),
            "added": list(self.added),
        }

    async def execute(self, query):
        if self.commit_completed and self.fail_post_commit_read:
            raise RuntimeError("secret post-commit read failure")
        sql = str(query)
        self.queries.append(sql)
        if "FOR UPDATE" in sql and not self._lock_change_applied and self.change_on_lock:
            self._lock_change_applied = True
            self.change_on_lock(self)

        if "FROM coincoin_users" in sql:
            return _EntityResult(sorted(self.users, key=lambda item: item.id))
        if "FROM coincoin_traffic_pack_balances" in sql:
            return _EntityResult(sorted(self.packs, key=lambda item: (item.user_id, item.id)))
        if "FROM coincoin_credit_balances" in sql:
            params = query.compile().params
            source_type = next(
                (value for key, value in params.items() if key.startswith("source_type") and isinstance(value, str)),
                None,
            )
            source_id = next(
                (value for key, value in params.items() if key.startswith("source_id") and isinstance(value, str)),
                None,
            )
            values = self.credits
            if source_type is not None and source_id is not None:
                values = [
                    item
                    for item in values
                    if item.source_type == source_type and item.source_id == source_id
                ]
            return _EntityResult(
                sorted(values, key=lambda item: (item.source_type, item.source_id, item.id))
            )
        raise AssertionError(f"unexpected query: {sql}")

    @property
    def no_autoflush(self):
        session = self

        @contextmanager
        def _scope():
            session.no_autoflush_enters += 1
            try:
                yield
            finally:
                session.no_autoflush_exits += 1

        return _scope()

    def add(self, item):
        self.added.append(item)
        self.credits.append(item)

    def expire_all(self):
        self.expire_all_calls += 1

    def begin_nested(self):
        session = self

        class _Nested:
            async def __aenter__(self):
                session.nested_begins += 1
                self.credit_count = len(session.credits)
                self.added_count = len(session.added)
                return self

            async def __aexit__(self, exc_type, _exc, _tb):
                if exc_type is not None:
                    del session.credits[self.credit_count :]
                    del session.added[self.added_count :]
                return False

        return _Nested()

    async def flush(self):
        self.flushes += 1
        if self.fail_flush_at == self.flushes:
            raise RuntimeError("simulated flush failure")

    async def commit(self):
        self.commits += 1
        if self.fail_commit:
            raise RuntimeError("secret commit failure")
        self.commit_completed = True
        self._snapshot = self._take_snapshot()
        if self.after_commit_mutation:
            self.after_commit_mutation(self)

    async def rollback(self):
        self.rollbacks += 1
        if self.fail_rollback:
            raise RuntimeError("secret rollback failure")
        for item, balance in self._snapshot["users"]:
            item.balance = balance
        for item, remaining, status in self._snapshot["packs"]:
            item.remaining_cents = remaining
            item.status = status
        self.credits[:] = self._snapshot["credits"]
        self.added[:] = self._snapshot["added"]


class CreditMigrationPlanTests(unittest.TestCase):
    def test_plans_positive_sources_skips_nonspendable_and_reports_debt_without_float_drift(self):
        users = [_user("u_2", -75), _user("u_1", 250), _user("u_0", 0)]
        packs = [
            _pack("pack_valid", user_id="u_1", remaining_cents=600),
            _pack("pack_expired", user_id="u_1", expires_at=AS_OF - timedelta(seconds=1)),
            _pack("pack_depleted", user_id="u_2", remaining_cents=0),
            _pack("pack_disabled", user_id="u_2", status="disabled", remaining_cents=300),
        ]

        report = migration.build_migration_plan(users, packs, [], as_of=AS_OF)
        payload = report.to_dict()

        self.assertEqual(
            [(item["source_type"], item["source_id"]) for item in payload["planned_items"]],
            [
                ("legacy_balance", "legacy_balance:u_1"),
                ("legacy_traffic_pack", "legacy_traffic_pack:pack_valid"),
            ],
        )
        self.assertEqual(payload["debts"], [{"user_id": "u_2", "retained_debt_cents": -75}])
        self.assertEqual(
            [item["reason"] for item in payload["skips"]],
            ["zero_balance", "depleted", "nonactive", "expired"],
        )
        totals = payload["totals"]
        self.assertEqual(payload["items"], payload["planned_items"])
        self.assertEqual(payload["source_totals"], totals)
        self.assertEqual(totals["observed_positive_source_cents"], 1750)
        self.assertEqual(totals["eligible_spendable_source_cents"], 850)
        self.assertEqual(totals["planned_credit_cents"], 850)
        self.assertEqual(totals["retained_eligible_conflict_cents"], 0)
        self.assertEqual(totals["retained_skipped_positive_cents"], 900)
        self.assertEqual(totals["retained_debt_signed_cents"], -75)
        self.assertEqual(totals["retained_debt_abs_cents"], 75)
        self.assertEqual(totals["already_migrated_credit_cents"], 0)
        self.assertEqual(totals["before_source_cents"], 1750)
        self.assertEqual(totals["retained_source_cents"], 900)
        self.assertEqual(payload["zero_drift"]["source_coverage_difference_cents"], 0)
        self.assertEqual(payload["zero_drift"]["migration_coverage_difference_cents"], 0)
        self.assertTrue(payload["zero_drift"]["conserved"])
        self.assertTrue(payload["apply_eligible"])
        self.assertEqual(payload["scanned"], 7)
        self.assertEqual(payload["planned"], 2)
        self.assertEqual(payload["limits"], {"max_scanned_rows": None, "max_planned_items": None})
        self.assertIn("full_table_lock_warning", payload)
        self.assertEqual(payload["safety"]["scanned"], 7)
        self.assertTrue(all(isinstance(value, int) for value in payload["source_totals"].values()))

    def test_invalid_pack_amount_or_unknown_status_blocks_apply(self):
        report = migration.build_migration_plan(
            [],
            [
                _pack("pack_negative", remaining_cents=-1),
                _pack("pack_unknown", status="mystery"),
                _pack("pack_bad_time", expires_at="tomorrow"),
            ],
            [],
            as_of=AS_OF,
        )

        self.assertFalse(report.apply_eligible)
        self.assertEqual(len(report.errors), 3)
        self.assertEqual(report.source_totals["planned_credit_cents"], 0)

    def test_raw_positive_pack_is_counted_even_when_original_amount_is_malformed(self):
        pack = _pack("pack_bad_original", remaining_cents=200)
        pack.original_cents = "1000"

        report = migration.build_migration_plan([], [pack], [], as_of=AS_OF)

        self.assertFalse(report.apply_eligible)
        self.assertEqual(report.source_totals["observed_positive_source_cents"], 200)
        self.assertEqual(report.source_totals["invalid_positive_source_cents"], 200)
        self.assertEqual(report.zero_drift["source_coverage_difference_cents"], 0)

    def test_retired_sources_with_matching_batches_are_already_migrated(self):
        users = [_user("u_1", 0)]
        packs = [_pack("pack_1", status="migrated", remaining_cents=0)]
        credits = [
            _credit(
                "cb_balance",
                user_id="u_1",
                source_type="legacy_balance",
                source_id="legacy_balance:u_1",
                product_id="legacy_balance",
                original_cents=250,
            ),
            _credit(
                "cb_pack",
                user_id="u_1",
                source_type="legacy_traffic_pack",
                source_id="legacy_traffic_pack:pack_1",
                product_id="traffic_10",
                original_cents=600,
            ),
        ]

        report = migration.build_migration_plan(users, packs, credits, as_of=AS_OF)

        self.assertEqual(report.counts["already_migrated"], 2)
        self.assertEqual(report.source_totals["already_migrated_credit_cents"], 850)
        self.assertEqual(report.planned_items, [])
        self.assertTrue(report.apply_eligible)

    def test_existing_conflicting_batch_retains_source_and_blocks_apply(self):
        users = [_user("u_1", 250)]
        credits = [
            _credit(
                "cb_wrong",
                user_id="u_1",
                source_type="legacy_balance",
                source_id="legacy_balance:u_1",
                product_id="legacy_balance",
                original_cents=249,
            )
        ]

        report = migration.build_migration_plan(users, [], credits, as_of=AS_OF)

        self.assertEqual(report.planned_items, [])
        self.assertEqual(report.counts["conflicts"], 1)
        self.assertFalse(report.apply_eligible)
        self.assertEqual(report.source_totals["observed_positive_source_cents"], 250)
        self.assertEqual(report.source_totals["eligible_spendable_source_cents"], 250)
        self.assertEqual(report.source_totals["planned_credit_cents"], 0)
        self.assertEqual(report.source_totals["retained_eligible_conflict_cents"], 250)
        self.assertEqual(report.zero_drift["migration_coverage_difference_cents"], 0)

    def test_duplicate_existing_source_is_a_conflict(self):
        users = [_user("u_1", 250)]
        duplicate = dict(
            user_id="u_1",
            source_type="legacy_balance",
            source_id="legacy_balance:u_1",
            product_id="legacy_balance",
            original_cents=250,
        )

        report = migration.build_migration_plan(
            users,
            [],
            [_credit("cb_1", **duplicate), _credit("cb_2", **duplicate)],
            as_of=AS_OF,
        )

        self.assertFalse(report.apply_eligible)
        self.assertEqual(report.counts["conflicts"], 1)
        self.assertEqual(report.conflicts[0]["reason"], "duplicate_credit_source")

    def test_orphan_malformed_and_status_inconsistent_legacy_credits_are_conflicts(self):
        credits = [
            _credit(
                "cb_orphan",
                user_id="missing",
                source_type="legacy_balance",
                source_id="legacy_balance:missing",
                product_id="legacy_balance",
                original_cents=100,
            ),
            _credit(
                "cb_malformed",
                user_id="u_1",
                source_type="legacy_balance",
                source_id="legacy_balance:",
                product_id="legacy_balance",
                original_cents=100,
            ),
            _credit(
                "cb_bad_state",
                user_id="u_1",
                source_type="legacy_traffic_pack",
                source_id="legacy_traffic_pack:pack_1",
                product_id="traffic_10",
                original_cents=100,
            ),
        ]
        pack = _pack("pack_1", status="depleted", remaining_cents=0)

        report = migration.build_migration_plan([_user("u_1", 0)], [pack], credits, as_of=AS_OF)

        self.assertFalse(report.apply_eligible)
        self.assertEqual(
            [item["reason"] for item in report.conflicts],
            ["malformed_credit_source", "orphan_credit_source", "credit_source_state_conflict"],
        )

    def test_migrated_pack_requires_exactly_one_matching_migration_batch(self):
        pack = _pack("pack_1", status="migrated", remaining_cents=0)

        missing = migration.build_migration_plan([], [pack], [], as_of=AS_OF)
        conflicting = migration.build_migration_plan(
            [],
            [pack],
            [
                _credit(
                    "cb_wrong",
                    user_id="u_1",
                    source_type="legacy_traffic_pack",
                    source_id="legacy_traffic_pack:pack_1",
                    product_id="wrong_product",
                    original_cents=600,
                )
            ],
            as_of=AS_OF,
        )

        self.assertFalse(missing.apply_eligible)
        self.assertEqual(missing.conflicts[0]["reason"], "missing_migration_batch")
        self.assertFalse(conflicting.apply_eligible)
        self.assertEqual(conflicting.conflicts[0]["reason"], "credit_source_terms_conflict")


class CreditMigrationDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_run_is_dry_run_and_never_commits_or_flushes(self):
        user = _user("u_1", 250)
        pack = _pack("pack_1", remaining_cents=600)
        db = _FakeSession(users=[user], packs=[pack])

        report = await migration.migrate_legacy_credits(db, as_of=AS_OF)

        self.assertEqual(report.mode, "dry_run")
        self.assertEqual(report.counts["planned"], 2)
        self.assertEqual(db.commits, 0)
        self.assertEqual(db.rollbacks, 0)
        self.assertEqual(db.flushes, 0)
        self.assertEqual(db.added, [])
        self.assertEqual(user.balance, 250)
        self.assertEqual(pack.remaining_cents, 600)
        self.assertFalse(any("FOR UPDATE" in query for query in db.queries))
        self.assertEqual(db.no_autoflush_enters, 1)
        self.assertEqual(db.no_autoflush_exits, 1)

    async def test_apply_grants_and_retires_scalar_and_pack_in_one_commit(self):
        user = _user("u_1", 250)
        pack = _pack("pack_1", remaining_cents=600)
        db = _FakeSession(users=[user], packs=[pack])

        report = await _apply(db)

        self.assertEqual(report.mode, "apply")
        self.assertEqual(db.commits, 1)
        self.assertEqual(db.rollbacks, 0)
        self.assertEqual(user.balance, 0)
        self.assertEqual(pack.remaining_cents, 0)
        self.assertEqual(pack.status, "migrated")
        self.assertEqual(
            [(item.source_type, item.source_id, item.original_cents) for item in db.added],
            [
                ("legacy_balance", "legacy_balance:u_1", 250),
                ("legacy_traffic_pack", "legacy_traffic_pack:pack_1", 600),
            ],
        )
        self.assertEqual(report.source_totals["retired_source_cents"], 850)
        self.assertEqual(report.source_totals["new_credit_cents"], 850)
        self.assertEqual(report.zero_drift["total_spendable_delta_cents"], 0)
        self.assertEqual(report.reconciliation["status"], "verified")
        self.assertEqual(db.no_autoflush_enters, 3)
        self.assertEqual(db.expire_all_calls, 1)
        locked_queries = [query for query in db.queries if "FOR UPDATE" in query]
        self.assertTrue(any("coincoin_users" in query for query in locked_queries))
        self.assertTrue(any("coincoin_traffic_pack_balances" in query for query in locked_queries))
        self.assertTrue(any("coincoin_credit_balances" in query for query in locked_queries))
        self.assertTrue(any("ORDER BY coincoin_users.id ASC" in query for query in locked_queries))
        self.assertTrue(
            any(
                "ORDER BY coincoin_traffic_pack_balances.user_id ASC, "
                "coincoin_traffic_pack_balances.id ASC" in query
                for query in locked_queries
            )
        )

    async def test_apply_rerun_is_idempotent(self):
        user = _user("u_1", 250)
        pack = _pack("pack_1", remaining_cents=600)
        db = _FakeSession(users=[user], packs=[pack])

        first = await _apply(db)
        second = await _apply(db)

        self.assertEqual(first.counts["planned"], 2)
        self.assertEqual(second.counts["planned"], 0)
        self.assertEqual(second.counts["already_migrated"], 2)
        self.assertEqual(len(db.added), 2)
        self.assertEqual(db.commits, 1)

    async def test_source_change_after_plan_refuses_apply_and_rolls_back(self):
        user = _user("u_1", 250)
        db = _FakeSession(
            users=[user],
            change_on_lock=lambda session: setattr(session.users[0], "balance", 251),
        )

        report = await _apply(db)

        self.assertEqual(report.mode, "apply_refused")
        self.assertFalse(report.apply_eligible)
        self.assertEqual(report.conflicts[-1]["reason"], "source_changed_after_plan")
        self.assertEqual(db.commits, 0)
        self.assertEqual(db.rollbacks, 1)
        self.assertEqual(user.balance, 250)
        self.assertEqual(db.added, [])

    async def test_grant_midpoint_failure_is_structured_and_rolls_back_all_mutations(self):
        user = _user("u_1", 250)
        pack = _pack("pack_1", remaining_cents=600)
        db = _FakeSession(users=[user], packs=[pack], fail_flush_at=2)

        report = await _apply(db)

        self.assertEqual(report.mode, "apply_failed")
        self.assertEqual(report.errors[-1]["reason"], "grant_failed")
        self.assertEqual(db.commits, 0)
        self.assertEqual(db.rollbacks, 1)
        self.assertEqual(user.balance, 250)
        self.assertEqual(pack.remaining_cents, 600)
        self.assertEqual(pack.status, "active")
        self.assertEqual(db.credits, [])
        self.assertEqual(db.added, [])

    async def test_final_flush_failure_is_structured_and_rolls_back(self):
        user = _user("u_1", 250)
        db = _FakeSession(users=[user], fail_flush_at=2)

        report = await _apply(db)

        self.assertEqual(report.mode, "apply_failed")
        self.assertEqual(report.errors[-1]["reason"], "flush_failed")
        self.assertEqual(user.balance, 250)
        self.assertEqual(db.credits, [])

    async def test_commit_failure_is_indeterminate_even_when_rollback_succeeds(self):
        db = _FakeSession(users=[_user("u_1", 250)], fail_commit=True)

        report = await _apply(db)

        self.assertEqual(report.mode, "apply_indeterminate")
        self.assertFalse(report.apply_eligible)
        self.assertEqual(report.errors[-1]["reason"], "commit_outcome_unknown")
        self.assertEqual(db.rollbacks, 1)

    async def test_rollback_failure_makes_precommit_failure_indeterminate(self):
        db = _FakeSession(users=[_user("u_1", 250)], fail_flush_at=1, fail_rollback=True)

        report = await _apply(db)

        self.assertEqual(report.mode, "apply_indeterminate")
        self.assertEqual(report.errors[-1]["reason"], "rollback_failed")

    async def test_post_commit_read_failure_is_indeterminate(self):
        db = _FakeSession(users=[_user("u_1", 250)], fail_post_commit_read=True)

        report = await _apply(db)

        self.assertEqual(report.mode, "apply_indeterminate")
        self.assertEqual(report.errors[-1]["reason"], "reconciliation_read_failed")
        self.assertEqual(db.commits, 1)
        self.assertEqual(db.rollbacks, 0)

    async def test_post_commit_mismatch_is_reconciliation_failed(self):
        def corrupt_source(session):
            session.users[0].balance = 1

        db = _FakeSession(users=[_user("u_1", 250)], after_commit_mutation=corrupt_source)

        report = await _apply(db)

        self.assertEqual(report.mode, "apply_indeterminate")
        self.assertEqual(report.errors[-1]["reason"], "reconciliation_failed")
        self.assertEqual(db.rollbacks, 0)

    async def test_orphan_credit_refuses_empty_apply_without_commit(self):
        orphan = _credit(
            "cb_orphan",
            user_id="missing",
            source_type="legacy_balance",
            source_id="legacy_balance:missing",
            product_id="legacy_balance",
            original_cents=100,
        )
        db = _FakeSession(credits=[orphan])

        report = await _apply(db)

        self.assertEqual(report.mode, "apply_refused")
        self.assertFalse(report.apply_eligible)
        self.assertEqual(db.commits, 0)

    async def test_missing_migrated_pack_batch_refuses_apply(self):
        db = _FakeSession(packs=[_pack("pack_1", status="migrated", remaining_cents=0)])

        report = await _apply(db)

        self.assertEqual(report.mode, "apply_refused")
        self.assertEqual(report.conflicts[0]["reason"], "missing_migration_batch")
        self.assertEqual(db.commits, 0)

    async def test_eligible_empty_apply_still_locks_and_rejects_concurrent_change(self):
        user = _user("u_1", 0)
        db = _FakeSession(
            users=[user],
            change_on_lock=lambda session: setattr(session.users[0], "balance", 100),
        )

        report = await _apply(db)

        self.assertEqual(report.mode, "apply_refused")
        self.assertEqual(report.conflicts[-1]["reason"], "source_changed_after_plan")
        self.assertEqual(db.rollbacks, 1)
        self.assertEqual(db.commits, 0)
        self.assertTrue(any("FOR UPDATE" in query for query in db.queries))

    async def test_stable_empty_apply_locks_then_returns_verified_noop_without_commit(self):
        db = _FakeSession(users=[_user("u_1", 0)])

        report = await _apply(db)

        self.assertEqual(report.mode, "apply")
        self.assertEqual(report.reconciliation["status"], "verified_noop")
        self.assertEqual(db.commits, 0)
        self.assertEqual(db.rollbacks, 1)
        self.assertTrue(any("FOR UPDATE" in query for query in db.queries))

    async def test_stable_empty_apply_rollback_failure_is_indeterminate(self):
        db = _FakeSession(users=[_user("u_1", 0)], fail_rollback=True)

        report = await _apply(db)

        self.assertEqual(report.mode, "apply_indeterminate")
        self.assertEqual(report.errors[-1]["reason"], "rollback_failed")
        self.assertEqual(db.commits, 0)

    async def test_apply_requires_limits_before_locking(self):
        db = _FakeSession(users=[_user("u_1", 250)])

        report = await migration.migrate_legacy_credits(db, as_of=AS_OF, apply=True)

        self.assertEqual(report.mode, "apply_refused")
        self.assertEqual(report.errors[-1]["reason"], "apply_limits_required")
        self.assertFalse(any("FOR UPDATE" in query for query in db.queries))
        self.assertEqual(db.commits, 0)

    async def test_apply_refuses_scanned_or_planned_limit_exceeded_before_locking(self):
        scanned_db = _FakeSession(users=[_user("u_1", 0), _user("u_2", 0)])
        planned_db = _FakeSession(users=[_user("u_1", 250), _user("u_2", 250)])

        scanned = await migration.migrate_legacy_credits(
            scanned_db,
            as_of=AS_OF,
            apply=True,
            max_scanned_rows=1,
            max_planned_items=10,
        )
        planned = await migration.migrate_legacy_credits(
            planned_db,
            as_of=AS_OF,
            apply=True,
            max_scanned_rows=10,
            max_planned_items=1,
        )

        self.assertEqual(scanned.errors[-1]["reason"], "apply_limit_exceeded")
        self.assertEqual(planned.errors[-1]["reason"], "apply_limit_exceeded")
        self.assertFalse(any("FOR UPDATE" in query for query in scanned_db.queries))
        self.assertFalse(any("FOR UPDATE" in query for query in planned_db.queries))

    async def test_real_sqlalchemy_session_pending_row_is_not_autoflushed_by_dry_run(self):
        from sqlalchemy import create_engine, event
        from sqlalchemy.orm import Session

        from app.models import CreditBalance, TrafficPackBalance, User

        engine = create_engine("sqlite:///:memory:")
        User.__table__.create(engine)
        TrafficPackBalance.__table__.create(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                """
                CREATE TABLE coincoin_credit_balances (
                    id VARCHAR(32) PRIMARY KEY,
                    user_id VARCHAR(32) NOT NULL,
                    source_type VARCHAR(32) NOT NULL,
                    source_id VARCHAR(128) NOT NULL,
                    product_id VARCHAR(64) NOT NULL,
                    status VARCHAR(16) NOT NULL,
                    original_cents BIGINT NOT NULL,
                    remaining_cents BIGINT NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        sync_session = Session(engine)
        pending = User(id="u_pending", username="pending", balance=100)
        sync_session.add(pending)
        flushes = []
        event.listen(sync_session, "before_flush", lambda *_args: flushes.append(True))

        class _SyncAdapter:
            @property
            def no_autoflush(self):
                return sync_session.no_autoflush

            async def execute(self, query):
                return sync_session.execute(query)

        state = await migration.load_legacy_state(_SyncAdapter())

        self.assertEqual(state.users, [])
        self.assertEqual(flushes, [])
        self.assertIn(pending, sync_session.new)
        sync_session.close()
        engine.dispose()

    async def test_real_sqlalchemy_reload_refreshes_stale_identity_map(self):
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import Session

        from app.models import CreditBalance, TrafficPackBalance, User

        engine = create_engine("sqlite:///:memory:")
        User.__table__.create(engine)
        TrafficPackBalance.__table__.create(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                """
                CREATE TABLE coincoin_credit_balances (
                    id VARCHAR(32) PRIMARY KEY,
                    user_id VARCHAR(32) NOT NULL,
                    source_type VARCHAR(32) NOT NULL,
                    source_id VARCHAR(128) NOT NULL,
                    product_id VARCHAR(64) NOT NULL,
                    status VARCHAR(16) NOT NULL,
                    original_cents BIGINT NOT NULL,
                    remaining_cents BIGINT NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        with Session(engine) as setup:
            setup.add(User(id="u_1", username="user-one", balance=100))
            setup.commit()

        primary = Session(engine, expire_on_commit=False)

        class _SyncAdapter:
            @property
            def no_autoflush(self):
                return primary.no_autoflush

            async def execute(self, query):
                return primary.execute(query)

        first = await migration.load_legacy_state(_SyncAdapter())
        self.assertEqual(first.users[0].balance, 100)
        primary.commit()

        with Session(engine) as external:
            external_user = external.execute(select(User).where(User.id == "u_1")).scalar_one()
            external_user.balance = 200
            external.commit()

        ordinary_reload = await migration.load_legacy_state(_SyncAdapter())
        self.assertEqual(ordinary_reload.users[0].balance, 100)
        locked = await migration.load_legacy_state(_SyncAdapter(), for_update=True)
        self.assertIs(locked.users[0], first.users[0])
        self.assertEqual(locked.users[0].balance, 200)
        primary.commit()

        with Session(engine) as external:
            external_user = external.execute(select(User).where(User.id == "u_1")).scalar_one()
            external_user.balance = 300
            external.commit()

        primary.expire_all()
        post_commit = await migration.load_legacy_state(_SyncAdapter())
        self.assertEqual(post_commit.users[0].balance, 300)
        primary.close()
        engine.dispose()

    async def test_migrate_refuses_dirty_real_session_without_select_or_flush(self):
        from sqlalchemy import create_engine, event, select
        from sqlalchemy.orm import Session

        from app.models import User

        engine = create_engine("sqlite:///:memory:")
        User.__table__.create(engine)
        with Session(engine) as setup:
            setup.add(User(id="u_1", username="user-one", balance=100))
            setup.commit()

        primary = Session(engine, expire_on_commit=False)
        user = primary.execute(select(User).where(User.id == "u_1")).scalar_one()
        user.balance = 999
        flushes = []
        event.listen(primary, "before_flush", lambda *_args: flushes.append(True))

        class _SyncAdapter:
            executes = 0

            @property
            def new(self):
                return primary.new

            @property
            def dirty(self):
                return primary.dirty

            @property
            def deleted(self):
                return primary.deleted

            async def execute(self, _query):
                self.executes += 1
                raise AssertionError("pending-session refusal must happen before SELECT")

        adapter = _SyncAdapter()
        report = await migration.migrate_legacy_credits(adapter, as_of=AS_OF)

        self.assertEqual(report.mode, "dry_run_refused")
        self.assertEqual(report.errors[0]["reason"], "session_has_pending_changes")
        self.assertEqual(adapter.executes, 0)
        self.assertEqual(flushes, [])
        self.assertEqual(user.balance, 999)
        self.assertIn(user, primary.dirty)
        with Session(engine) as external:
            stored = external.execute(select(User).where(User.id == "u_1")).scalar_one()
            self.assertEqual(stored.balance, 100)
        primary.close()
        engine.dispose()


class CreditMigrationCliTests(unittest.TestCase):
    def test_cli_defaults_to_dry_run_and_requires_explicit_apply(self):
        defaults = migration.parse_args([])
        applying = migration.parse_args(
            [
                "--apply",
                "--max-scanned-rows",
                "100",
                "--max-planned-items",
                "10",
                "--json",
            ]
        )

        self.assertFalse(defaults.apply)
        self.assertFalse(defaults.json)
        self.assertTrue(applying.apply)
        self.assertTrue(applying.json)
        self.assertEqual(applying.max_scanned_rows, 100)
        self.assertEqual(applying.max_planned_items, 10)

    def test_human_summary_contains_mode_counts_totals_and_eligibility(self):
        report = migration.build_migration_plan([_user("u_1", 250)], [], [], as_of=AS_OF)

        summary = migration.render_human_summary(report)

        self.assertIn("mode: dry_run", summary)
        self.assertIn("planned: 1", summary)
        self.assertIn("planned_credit_cents: 250", summary)
        self.assertIn("apply_eligible: yes", summary)
        self.assertIn("planned_items:", summary)
        self.assertIn("skips:", summary)
        self.assertIn("full_table_lock_warning", summary)

    def test_help_needs_no_database_configuration(self):
        script = Path(migration.__file__).resolve()
        env = os.environ.copy()
        for name in list(env):
            if name == "COINCOIN_DATABASE_URL" or name.startswith("COINCOIN_DB_"):
                env.pop(name)

        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=script.parents[1],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--apply", result.stdout)
        self.assertIn("--max-scanned-rows", result.stdout)
        self.assertIn("--max-planned-items", result.stdout)
        self.assertIn("locks all scanned", result.stdout.lower())
        self.assertIn("dry-run", result.stdout.lower())

    def test_cli_apply_without_limits_is_nonzero_json_and_does_not_open_database(self):
        stdout = io.StringIO()
        run_cli = AsyncMock()
        with patch.object(migration, "_run_cli", run_cli), redirect_stdout(stdout):
            exit_code = migration.main(["--apply", "--json"])

        payload = json.loads(stdout.getvalue())
        self.assertNotEqual(exit_code, 0)
        self.assertEqual(payload["mode"], "apply_refused")
        self.assertEqual(payload["errors"][0]["reason"], "apply_limits_required")
        run_cli.assert_not_awaited()

    def test_json_cli_sanitizes_expected_database_exception(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(
            migration,
            "_run_cli",
            AsyncMock(side_effect=RuntimeError("mysql://admin:secret@example/db")),
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = migration.main(["--json"])

        payload = json.loads(stdout.getvalue())
        self.assertNotEqual(exit_code, 0)
        self.assertEqual(payload["mode"], "dry_run_failed")
        self.assertEqual(payload["errors"][0]["reason"], "database_operation_failed")
        self.assertNotIn("secret", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
