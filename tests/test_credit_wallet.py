import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable

from app import main as main_module
from app.main import _is_index_already_exists_error
from app.credit_wallet import (
    CreditSourceConflictError,
    CreditWalletError,
    InsufficientCreditError,
    debit_credit_batches,
    grant_permanent_credit,
    list_spendable_credit_batches,
    refund_credit_allocations,
    total_spendable_credit_cents,
)
from app.models import CreditAllocation, CreditBalance


class _EntityResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value


class _FakeDB:
    def __init__(self, execute_results=None, flush_results=None):
        self.execute_results = list(execute_results or [])
        self.flush_results = list(flush_results or [])
        self.queries = []
        self.added = []
        self.flushes = 0
        self.nested_begins = 0
        self.nested_rollbacks = 0
        self.outer_rollbacks = 0

    async def execute(self, query):
        self.queries.append(query)
        if not self.execute_results:
            raise AssertionError("unexpected execute call")
        return self.execute_results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    def begin_nested(self):
        db = self

        class _NestedTransaction:
            async def __aenter__(self):
                db.nested_begins += 1
                self.added_count = len(db.added)
                return self

            async def __aexit__(self, exc_type, _exc, _traceback):
                if exc_type is not None:
                    db.nested_rollbacks += 1
                    del db.added[self.added_count:]
                return False

        return _NestedTransaction()

    async def flush(self):
        self.flushes += 1
        if self.flush_results:
            result = self.flush_results.pop(0)
            if isinstance(result, BaseException):
                raise result

    async def rollback(self):
        self.outer_rollbacks += 1


def _balance(
    balance_id: str,
    *,
    remaining_cents: int,
    original_cents: int | None = None,
    created_at: datetime | None = None,
):
    return SimpleNamespace(
        id=balance_id,
        user_id="u_1",
        source_type="payment_order",
        source_id=f"order_{balance_id}",
        product_id="credit_basic",
        status="active" if remaining_cents else "depleted",
        original_cents=original_cents if original_cents is not None else remaining_cents,
        remaining_cents=remaining_cents,
        created_at=created_at or datetime(2026, 7, 1),
    )


class CreditWalletTests(unittest.IsolatedAsyncioTestCase):
    async def test_grant_is_idempotent_by_source(self):
        db = _FakeDB(execute_results=[_EntityResult(None)])

        granted = await grant_permanent_credit(
            db,
            user_id="u_1",
            source_type="payment_order",
            source_id="CC_1",
            amount_cents=10000,
            product_id="credit_basic",
        )

        self.assertIsInstance(granted, CreditBalance)
        self.assertEqual(granted.original_cents, 10000)
        self.assertEqual(granted.remaining_cents, 10000)
        self.assertEqual(len(db.added), 1)
        self.assertEqual(db.flushes, 1)
        self.assertEqual(db.nested_begins, 1)

        replay_db = _FakeDB(execute_results=[_EntityResult(granted)])
        replayed = await grant_permanent_credit(
            replay_db,
            user_id="u_1",
            source_type="payment_order",
            source_id="CC_1",
            amount_cents=10000,
            product_id="credit_basic",
        )

        self.assertIs(replayed, granted)
        self.assertEqual(replay_db.added, [])

    async def test_grant_recovers_unique_source_race_inside_savepoint(self):
        winner = _balance("cb_winner", remaining_cents=10000, original_cents=10000)
        winner.source_id = "CC_race"
        duplicate_error = IntegrityError(
            "INSERT INTO coincoin_credit_balances",
            {},
            Exception(1062, "Duplicate entry for key 'uq_credit_balances_source'"),
        )
        db = _FakeDB(
            execute_results=[_EntityResult(None), _EntityResult(winner)],
            flush_results=[duplicate_error],
        )

        result = await grant_permanent_credit(
            db,
            user_id="u_1",
            source_type="payment_order",
            source_id="CC_race",
            amount_cents=10000,
            product_id="credit_basic",
        )

        self.assertIs(result, winner)
        self.assertEqual(db.flushes, 1)
        self.assertEqual(db.nested_begins, 1)
        self.assertEqual(db.nested_rollbacks, 1)
        self.assertEqual(db.outer_rollbacks, 0)
        self.assertEqual(db.added, [])
        self.assertNotIn("FOR UPDATE", str(db.queries[0]))
        self.assertIn("FOR UPDATE", str(db.queries[1]))
        self.assertIn(
            "ORDER BY coincoin_credit_balances.created_at ASC, coincoin_credit_balances.id ASC",
            str(db.queries[1]),
        )

    async def test_grant_does_not_swallow_unrelated_integrity_error(self):
        primary_key_error = IntegrityError(
            "INSERT INTO coincoin_credit_balances",
            {},
            Exception(1062, "Duplicate entry for key 'PRIMARY'"),
        )
        db = _FakeDB(
            execute_results=[_EntityResult(None)],
            flush_results=[primary_key_error],
        )

        with self.assertRaises(IntegrityError) as raised:
            await grant_permanent_credit(
                db,
                user_id="u_1",
                source_type="payment_order",
                source_id="CC_primary_collision",
                amount_cents=10000,
                product_id="credit_basic",
            )

        self.assertIs(raised.exception, primary_key_error)
        self.assertEqual(len(db.queries), 1)
        self.assertEqual(db.nested_rollbacks, 1)
        self.assertEqual(db.outer_rollbacks, 0)

    async def test_grant_validates_winning_row_after_unique_source_race(self):
        winner = _balance("cb_wrong_winner", remaining_cents=10000, original_cents=10000)
        winner.source_id = "CC_wrong_winner"
        winner.user_id = "u_other"
        duplicate_error = IntegrityError(
            "INSERT INTO coincoin_credit_balances",
            {},
            Exception(1062, "Duplicate entry for key 'uq_credit_balances_source'"),
        )
        db = _FakeDB(
            execute_results=[_EntityResult(None), _EntityResult(winner)],
            flush_results=[duplicate_error],
        )

        with self.assertRaises(CreditSourceConflictError):
            await grant_permanent_credit(
                db,
                user_id="u_1",
                source_type="payment_order",
                source_id="CC_wrong_winner",
                amount_cents=10000,
                product_id="credit_basic",
            )

        self.assertEqual(len(db.queries), 2)
        self.assertIn("FOR UPDATE", str(db.queries[1]))
        self.assertEqual(db.outer_rollbacks, 0)

    async def test_grant_rejects_replayed_source_with_conflicting_terms(self):
        existing = _balance("cb_existing", remaining_cents=9000, original_cents=10000)
        existing.source_id = "CC_conflict"

        for changed_terms in [
            {"user_id": "u_2"},
            {"amount_cents": 10001},
            {"product_id": "credit_large"},
        ]:
            kwargs = {
                "user_id": "u_1",
                "source_type": "payment_order",
                "source_id": "CC_conflict",
                "amount_cents": 10000,
                "product_id": "credit_basic",
                **changed_terms,
            }
            db = _FakeDB(execute_results=[_EntityResult(existing)])
            with self.subTest(changed_terms=changed_terms):
                with self.assertRaises(CreditSourceConflictError):
                    await grant_permanent_credit(db, **kwargs)
            self.assertEqual(db.flushes, 0)
            self.assertEqual(db.added, [])

    async def test_grant_rejects_non_integer_cents(self):
        db = _FakeDB()

        with self.assertRaises(TypeError):
            await grant_permanent_credit(
                db,
                user_id="u_1",
                source_type="payment_order",
                source_id="CC_float",
                amount_cents=10.5,
            )

        self.assertEqual(db.queries, [])
        self.assertEqual(db.added, [])

    async def test_list_and_total_spendable_batches(self):
        first = _balance("cb_1", remaining_cents=125)
        second = _balance("cb_2", remaining_cents=375)
        db = _FakeDB(
            execute_results=[
                _EntityResult([first, second]),
                _EntityResult([first, second]),
            ]
        )

        batches = await list_spendable_credit_batches(db, "u_1")
        total = await total_spendable_credit_cents(db, "u_1")

        self.assertEqual([batch.id for batch in batches], ["cb_1", "cb_2"])
        self.assertEqual(total, 500)

    async def test_fifo_debit_returns_multi_batch_allocation_payload(self):
        first = _balance("cb_old", remaining_cents=100, created_at=datetime(2026, 7, 1))
        second = _balance("cb_new", remaining_cents=200, created_at=datetime(2026, 7, 2))
        db = _FakeDB(execute_results=[_EntityResult([first, second])])

        result = await debit_credit_batches(db, user_id="u_1", amount_cents=250)

        self.assertEqual(result["debited_cents"], 250)
        self.assertEqual(first.remaining_cents, 0)
        self.assertEqual(first.status, "depleted")
        self.assertEqual(second.remaining_cents, 50)
        self.assertEqual(second.status, "active")
        self.assertEqual(len(db.added), 2)
        self.assertTrue(all(isinstance(item, CreditAllocation) for item in db.added))
        self.assertEqual(
            [
                (item["credit_balance_id"], item["amount_cents"])
                for item in result["allocations"]
            ],
            [("cb_old", 100), ("cb_new", 150)],
        )
        query_sql = str(db.queries[0])
        self.assertIn(
            "ORDER BY coincoin_credit_balances.created_at ASC, coincoin_credit_balances.id ASC",
            query_sql,
        )
        self.assertIn("FOR UPDATE", query_sql)

    async def test_insufficient_balance_does_not_partially_mutate(self):
        first = _balance("cb_1", remaining_cents=100)
        second = _balance("cb_2", remaining_cents=25)
        db = _FakeDB(execute_results=[_EntityResult([first, second])])

        with self.assertRaises(InsufficientCreditError):
            await debit_credit_batches(db, user_id="u_1", amount_cents=126)

        self.assertEqual(first.remaining_cents, 100)
        self.assertEqual(first.status, "active")
        self.assertEqual(second.remaining_cents, 25)
        self.assertEqual(second.status, "active")
        self.assertEqual(db.added, [])

    async def test_refund_restores_exact_allocations_once_for_simulated_waiter(self):
        first = _balance("cb_1", remaining_cents=0, original_cents=100)
        second = _balance("cb_2", remaining_cents=50, original_cents=200)
        allocation_1 = SimpleNamespace(
            id="ca_1",
            user_id="u_1",
            credit_balance_id="cb_1",
            amount_cents=100,
            refunded_at=None,
            created_at=datetime(2026, 7, 3),
        )
        allocation_2 = SimpleNamespace(
            id="ca_2",
            user_id="u_1",
            credit_balance_id="cb_2",
            amount_cents=150,
            refunded_at=None,
            created_at=datetime(2026, 7, 3),
        )
        db = _FakeDB(
            execute_results=[
                _EntityResult([allocation_1, allocation_2]),
                _EntityResult([first, second]),
                _EntityResult([allocation_1, allocation_2]),
            ]
        )

        refunded = await refund_credit_allocations(
            db,
            user_id="u_1",
            allocation_ids=["ca_1", "ca_2"],
            now=datetime(2026, 7, 4, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )
        replayed = await refund_credit_allocations(
            db,
            user_id="u_1",
            allocation_ids=["ca_1", "ca_2"],
        )

        self.assertEqual(refunded["refunded_cents"], 250)
        self.assertEqual(replayed["refunded_cents"], 0)
        self.assertEqual(first.remaining_cents, 100)
        self.assertEqual(first.status, "active")
        self.assertEqual(second.remaining_cents, 200)
        self.assertEqual(second.status, "active")
        self.assertIsNotNone(allocation_1.refunded_at)
        self.assertIsNotNone(allocation_2.refunded_at)
        self.assertEqual(allocation_1.refunded_at, datetime(2026, 7, 4, 0, 0))
        self.assertIsNone(allocation_1.refunded_at.tzinfo)
        self.assertEqual(len(db.queries), 3)
        allocation_lock_sql = str(db.queries[0])
        balance_lock_sql = str(db.queries[1])
        replay_lock_sql = str(db.queries[2])
        self.assertIn(
            "ORDER BY coincoin_credit_allocations.created_at ASC, coincoin_credit_allocations.id ASC",
            allocation_lock_sql,
        )
        self.assertIn("FOR UPDATE", allocation_lock_sql)
        self.assertIn(
            "ORDER BY coincoin_credit_balances.created_at ASC, coincoin_credit_balances.id ASC",
            balance_lock_sql,
        )
        self.assertIn("FOR UPDATE", balance_lock_sql)
        self.assertIn("FOR UPDATE", replay_lock_sql)

    async def test_strict_refund_rejects_refunded_or_mismatched_allocation_metadata_without_mutation(self):
        scenarios = [
            {
                "name": "partially_refunded",
                "actual_refunded_at": datetime(2026, 7, 4),
                "expected_balance_id": "cb_1",
                "expected_amount": 100,
            },
            {
                "name": "amount_mismatch",
                "actual_refunded_at": None,
                "expected_balance_id": "cb_1",
                "expected_amount": 99,
            },
            {
                "name": "balance_id_mismatch",
                "actual_refunded_at": None,
                "expected_balance_id": "cb_other",
                "expected_amount": 100,
            },
        ]
        for scenario in scenarios:
            with self.subTest(name=scenario["name"]):
                balance = CreditBalance(
                    id="cb_1",
                    user_id="u_1",
                    source_type="payment_order",
                    source_id="order_1",
                    product_id="credit_light",
                    status="depleted",
                    original_cents=100,
                    remaining_cents=0,
                    created_at=datetime(2026, 7, 1),
                )
                allocation = CreditAllocation(
                    id="ca_1",
                    user_id="u_1",
                    credit_balance_id="cb_1",
                    amount_cents=100,
                    refunded_at=scenario["actual_refunded_at"],
                    created_at=datetime(2026, 7, 3),
                )
                db = _FakeDB(
                    execute_results=[
                        _EntityResult([allocation]),
                        _EntityResult([balance]),
                    ]
                )

                with self.assertRaises(CreditWalletError):
                    await refund_credit_allocations(
                        db,
                        user_id="u_1",
                        allocation_ids=["ca_1"],
                        expected_allocations=[
                            {
                                "allocation_id": "ca_1",
                                "credit_balance_id": scenario["expected_balance_id"],
                                "amount_cents": scenario["expected_amount"],
                            }
                        ],
                    )

                self.assertEqual(balance.remaining_cents, 0)
                self.assertEqual(balance.status, "depleted")
                self.assertEqual(allocation.refunded_at, scenario["actual_refunded_at"])

    async def test_startup_migrations_include_credit_tables_and_indexes(self):
        class _MigrationConn:
            def __init__(self):
                self.statements = []

            async def execute(self, statement):
                self.statements.append(str(statement))

        conn = _MigrationConn()
        await main_module._run_migrations(conn)
        sql = "\n".join(conn.statements)

        self.assertIn("CREATE TABLE coincoin_credit_balances", sql)
        self.assertIn("CREATE TABLE coincoin_credit_allocations", sql)
        self.assertIn("UNIQUE KEY uq_credit_balances_source (source_type, source_id)", sql)
        self.assertIn(
            "CREATE UNIQUE INDEX uq_credit_balances_source ON coincoin_credit_balances (source_type, source_id)",
            sql,
        )
        self.assertIn(
            "CREATE INDEX ix_credit_balances_user_spendable ON coincoin_credit_balances (user_id, status, created_at, id)",
            sql,
        )
        self.assertIn(
            "CREATE INDEX ix_credit_allocations_user_created ON coincoin_credit_allocations (user_id, created_at)",
            sql,
        )

    async def test_credit_schema_orm_and_raw_ddl_are_in_parity(self):
        class _MigrationConn:
            def __init__(self):
                self.statements = []

            async def execute(self, statement):
                self.statements.append(str(statement).strip())

        conn = _MigrationConn()
        await main_module._run_migrations(conn)
        balance_ddl = next(
            sql for sql in conn.statements if sql.startswith("CREATE TABLE coincoin_credit_balances")
        )
        allocation_ddl = next(
            sql for sql in conn.statements if sql.startswith("CREATE TABLE coincoin_credit_allocations")
        )

        self.assertEqual(list(CreditBalance.__table__.foreign_key_constraints), [])
        self.assertEqual(list(CreditAllocation.__table__.foreign_key_constraints), [])
        self.assertNotIn("FOREIGN KEY", balance_ddl)
        self.assertNotIn("FOREIGN KEY", allocation_ddl)

        expected_balance_columns = {
            "id": (False, None, "id VARCHAR(32) PRIMARY KEY"),
            "user_id": (False, None, "user_id VARCHAR(32) NOT NULL"),
            "source_type": (False, "''", "source_type VARCHAR(32) NOT NULL DEFAULT ''"),
            "source_id": (False, "''", "source_id VARCHAR(128) NOT NULL DEFAULT ''"),
            "product_id": (False, "''", "product_id VARCHAR(64) NOT NULL DEFAULT ''"),
            "status": (False, "'active'", "status VARCHAR(16) NOT NULL DEFAULT 'active'"),
            "original_cents": (False, None, "original_cents BIGINT NOT NULL"),
            "remaining_cents": (False, None, "remaining_cents BIGINT NOT NULL"),
            "created_at": (
                False,
                "CURRENT_TIMESTAMP",
                "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP",
            ),
            "updated_at": (
                False,
                "CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
                "updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
            ),
        }
        expected_allocation_columns = {
            "id": (False, None, "id VARCHAR(32) PRIMARY KEY"),
            "user_id": (False, None, "user_id VARCHAR(32) NOT NULL"),
            "credit_balance_id": (False, None, "credit_balance_id VARCHAR(32) NOT NULL"),
            "amount_cents": (False, None, "amount_cents BIGINT NOT NULL"),
            "refunded_at": (True, None, "refunded_at DATETIME NULL"),
            "created_at": (
                False,
                "CURRENT_TIMESTAMP",
                "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP",
            ),
        }
        for table, ddl, expected in [
            (CreditBalance.__table__, balance_ddl, expected_balance_columns),
            (CreditAllocation.__table__, allocation_ddl, expected_allocation_columns),
        ]:
            for name, (nullable, server_default, ddl_fragment) in expected.items():
                column = table.c[name]
                self.assertEqual(column.nullable, nullable, name)
                actual_default = None if column.server_default is None else str(column.server_default.arg)
                self.assertEqual(actual_default, server_default, name)
                self.assertIn(ddl_fragment, ddl)

        self.assertIsNotNone(CreditBalance.__table__.c.updated_at.server_onupdate)
        unique_constraints = {
            constraint.name: tuple(column.name for column in constraint.columns)
            for constraint in CreditBalance.__table__.constraints
            if constraint.name
        }
        self.assertEqual(
            unique_constraints["uq_credit_balances_source"],
            ("source_type", "source_id"),
        )
        self.assertIn("UNIQUE KEY uq_credit_balances_source (source_type, source_id)", balance_ddl)

        expected_checks = {
            "ck_credit_balances_original_positive": "original_cents > 0",
            "ck_credit_balances_remaining_range": (
                "remaining_cents >= 0 AND remaining_cents <= original_cents"
            ),
            "ck_credit_balances_status": "status IN ('active', 'depleted')",
        }
        actual_balance_checks = {
            constraint.name: str(constraint.sqltext)
            for constraint in CreditBalance.__table__.constraints
            if constraint.name and constraint.name.startswith("ck_credit_balances_")
        }
        self.assertEqual(actual_balance_checks, expected_checks)
        actual_allocation_checks = {
            constraint.name: str(constraint.sqltext)
            for constraint in CreditAllocation.__table__.constraints
            if constraint.name and constraint.name.startswith("ck_credit_allocations_")
        }
        self.assertEqual(
            actual_allocation_checks,
            {"ck_credit_allocations_amount_positive": "amount_cents > 0"},
        )
        for name, expression in expected_checks.items():
            self.assertIn(f"CONSTRAINT {name} CHECK ({expression})", balance_ddl)
        self.assertIn(
            "CONSTRAINT ck_credit_allocations_amount_positive CHECK (amount_cents > 0)",
            allocation_ddl,
        )

        compiled_balance = str(
            CreateTable(CreditBalance.__table__).compile(dialect=mysql.dialect())
        )
        compiled_allocation = str(
            CreateTable(CreditAllocation.__table__).compile(dialect=mysql.dialect())
        )
        for name in expected_checks:
            self.assertIn(f"CONSTRAINT {name} CHECK", compiled_balance)
        self.assertIn(
            "CONSTRAINT ck_credit_allocations_amount_positive CHECK",
            compiled_allocation,
        )

        expected_indexes = {
            "ix_credit_balances_user_id": ("user_id",),
            "ix_credit_balances_product_id": ("product_id",),
            "ix_credit_balances_status": ("status",),
            "ix_credit_balances_user_spendable": ("user_id", "status", "created_at", "id"),
            "ix_credit_allocations_user_id": ("user_id",),
            "ix_credit_allocations_credit_balance_id": ("credit_balance_id",),
            "ix_credit_allocations_refunded_at": ("refunded_at",),
            "ix_credit_allocations_user_created": ("user_id", "created_at"),
            "ix_credit_allocations_balance_created": ("credit_balance_id", "created_at"),
        }
        orm_indexes = {
            index.name: tuple(column.name for column in index.columns)
            for table in (CreditBalance.__table__, CreditAllocation.__table__)
            for index in table.indexes
        }
        self.assertEqual(orm_indexes, expected_indexes)
        migration_sql = "\n".join(conn.statements)
        for index_name, columns in expected_indexes.items():
            table_name = (
                "coincoin_credit_balances"
                if index_name.startswith("ix_credit_balances_")
                else "coincoin_credit_allocations"
            )
            self.assertIn(
                f"CREATE INDEX {index_name} ON {table_name} ({', '.join(columns)})",
                migration_sql,
            )

    async def test_credit_startup_migrations_are_restart_safe(self):
        class _RestartMigrationConn:
            def __init__(self):
                self.seen = set()
                self.credit_attempts = []

            async def execute(self, statement):
                sql = str(statement).strip()
                is_credit_table = sql.startswith("CREATE TABLE coincoin_credit_")
                is_credit_index = (
                    sql.startswith("CREATE INDEX ix_credit_")
                    or sql.startswith("CREATE UNIQUE INDEX uq_credit_")
                )
                if not (is_credit_table or is_credit_index):
                    return
                self.credit_attempts.append(sql)
                if sql in self.seen:
                    if is_credit_table:
                        raise RuntimeError("Table already exists")
                    raise RuntimeError("Duplicate key name")
                self.seen.add(sql)

        conn = _RestartMigrationConn()
        await main_module._run_migrations(conn)
        await main_module._run_migrations(conn)

        unique_attempts = set(conn.credit_attempts)
        self.assertTrue(unique_attempts)
        self.assertTrue(all(conn.credit_attempts.count(sql) == 2 for sql in unique_attempts))

    def test_index_exists_error_classification_is_precise(self):
        class _WrappedError(RuntimeError):
            def __init__(self, errno, message):
                super().__init__(message)
                self.orig = Exception(errno, message)

        self.assertTrue(
            _is_index_already_exists_error(_WrappedError(1061, "Duplicate key name 'ix_a'"))
        )
        self.assertFalse(
            _is_index_already_exists_error(_WrappedError(1062, "Duplicate entry 'x'"))
        )
        self.assertTrue(_is_index_already_exists_error(RuntimeError("Duplicate key name 'ix_a'")))
        self.assertTrue(_is_index_already_exists_error(RuntimeError("index already exists")))
        self.assertFalse(_is_index_already_exists_error(RuntimeError("Duplicate entry 'x'")))

    async def test_source_unique_index_data_conflict_aborts_startup(self):
        class _WrappedError(RuntimeError):
            def __init__(self):
                super().__init__("Duplicate entry 'payment_order-CC_1'")
                self.orig = Exception(1062, "Duplicate entry 'payment_order-CC_1'")

        class _MigrationConn:
            async def execute(self, statement):
                if str(statement).strip().startswith(
                    "CREATE UNIQUE INDEX uq_credit_balances_source"
                ):
                    raise _WrappedError()

        with self.assertRaises(_WrappedError):
            await main_module._run_migrations(_MigrationConn())

    async def test_source_unique_index_string_and_other_failures_abort_startup(self):
        for error in [
            RuntimeError("Duplicate entry 'payment_order-CC_1'"),
            RuntimeError("permission denied while creating index"),
        ]:
            class _MigrationConn:
                async def execute(self, statement):
                    if str(statement).strip().startswith(
                        "CREATE UNIQUE INDEX uq_credit_balances_source"
                    ):
                        raise error

            with self.subTest(error=error):
                with self.assertRaises(RuntimeError) as raised:
                    await main_module._run_migrations(_MigrationConn())
                self.assertIs(raised.exception, error)

    async def test_ordinary_index_duplicate_name_remains_restart_safe(self):
        class _WrappedError(RuntimeError):
            def __init__(self, errno, message):
                super().__init__(message)
                self.orig = Exception(errno, message)

        target = "CREATE INDEX ix_request_logs_created_at"
        for error in [
            _WrappedError(1061, "Duplicate key name 'ix_request_logs_created_at'"),
            RuntimeError("Duplicate key name 'ix_request_logs_created_at'"),
        ]:
            class _MigrationConn:
                async def execute(self, statement):
                    if str(statement).strip().startswith(target):
                        raise error

            with self.subTest(error=error):
                await main_module._run_migrations(_MigrationConn())


if __name__ == "__main__":
    unittest.main()
