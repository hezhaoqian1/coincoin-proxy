import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import app.openai_compat as openai_module
import app.admin as admin_module
import app.rate_limiter as rate_limiter_module
from app.schemas import RedemptionCodeUpdateRequest, RedemptionGenerateRequest


class _FakeEntityResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        if self._value is None:
            raise AssertionError("expected entity, got None")
        return self._value


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeDB:
    def __init__(self, execute_results):
        self._execute_results = list(execute_results)
        self.queries = []
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, query):
        self.queries.append(query)
        if not self._execute_results:
            raise AssertionError("unexpected execute call")
        return self._execute_results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class _FakeRequest:
    def __init__(self, code):
        self._code = code

    async def json(self):
        return {"code": self._code}


class RedemptionCodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_single_use_code_marks_used(self):
        code = SimpleNamespace(
            id="rc_1",
            code="CC-AAAA-BBBB-CCCC-DDDD",
            balance_cents=1000,
            status="unused",
            max_redemptions=1,
            per_user_limit=1,
            redemption_count=0,
            used_by=None,
            used_at=None,
        )
        user = SimpleNamespace(id="u_1", balance=250)
        db = _FakeDB([
            _FakeEntityResult(code),
            _FakeScalarResult(0),
            _FakeEntityResult(user),
        ])

        with patch.object(openai_module, "authenticate_user", AsyncMock(return_value=SimpleNamespace(id="u_1"))), \
                patch.object(rate_limiter_module.rate_limiter, "allow", AsyncMock(return_value=True)), \
                patch.object(openai_module, "ensure_finance_summary_initialized", AsyncMock()), \
                patch.object(openai_module, "increment_finance_summary", AsyncMock()):
            result = await openai_module.redeem_code(_FakeRequest(code.code), db)

        self.assertTrue(result["success"])
        self.assertEqual(result["added_cents"], 1000)
        self.assertEqual(user.balance, 1250)
        self.assertEqual(code.status, "used")
        self.assertEqual(code.redemption_count, 1)
        self.assertEqual(code.used_by, "u_1")
        self.assertEqual(len(db.added), 1)
        self.assertEqual(db.commits, 1)

    async def test_campaign_code_is_unlimited_but_once_per_user(self):
        code = SimpleNamespace(
            id="rc_campaign",
            code="libertytalk0607",
            balance_cents=10000,
            status="active",
            max_redemptions=0,
            per_user_limit=1,
            redemption_count=42,
            used_by=None,
            used_at=None,
        )
        user = SimpleNamespace(id="u_2", balance=0)
        db = _FakeDB([
            _FakeEntityResult(code),
            _FakeScalarResult(0),
            _FakeEntityResult(user),
        ])

        with patch.object(openai_module, "authenticate_user", AsyncMock(return_value=SimpleNamespace(id="u_2"))), \
                patch.object(rate_limiter_module.rate_limiter, "allow", AsyncMock(return_value=True)), \
                patch.object(openai_module, "ensure_finance_summary_initialized", AsyncMock()), \
                patch.object(openai_module, "increment_finance_summary", AsyncMock()):
            result = await openai_module.redeem_code(_FakeRequest("libertytalk0607"), db)

        self.assertEqual(result["added_cents"], 10000)
        self.assertEqual(user.balance, 10000)
        self.assertEqual(code.status, "active")
        self.assertEqual(code.redemption_count, 43)
        self.assertEqual(code.used_by, "u_2")
        self.assertEqual(len(db.added), 1)

    async def test_campaign_code_rejects_second_use_by_same_user(self):
        code = SimpleNamespace(
            id="rc_campaign",
            code="libertytalk0607",
            balance_cents=10000,
            status="active",
            max_redemptions=0,
            per_user_limit=1,
            redemption_count=42,
            used_by=None,
            used_at=None,
        )
        db = _FakeDB([
            _FakeEntityResult(code),
            _FakeScalarResult(1),
        ])

        with patch.object(openai_module, "authenticate_user", AsyncMock(return_value=SimpleNamespace(id="u_2"))), \
                patch.object(rate_limiter_module.rate_limiter, "allow", AsyncMock(return_value=True)):
            with self.assertRaises(HTTPException) as ctx:
                await openai_module.redeem_code(_FakeRequest("libertytalk0607"), db)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("already redeemed", ctx.exception.detail)
        self.assertEqual(code.redemption_count, 42)
        self.assertEqual(db.commits, 0)


class AdminRedemptionCodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_can_create_custom_campaign_code(self):
        db = _FakeDB([_FakeEntityResult(None)])
        payload = RedemptionGenerateRequest(
            count=1,
            code="libertytalk0607",
            balance_cents=10000,
            max_redemptions=0,
            per_user_limit=1,
            note="LibertyTalk 0607 campaign",
        )

        result = await admin_module.generate_redemption_codes(payload, db)

        self.assertEqual(result.codes, ["libertytalk0607"])
        self.assertEqual(result.balance_cents, 10000)
        self.assertEqual(result.max_redemptions, 0)
        self.assertEqual(result.per_user_limit, 1)
        self.assertEqual(db.commits, 1)
        self.assertEqual(len(db.added), 1)
        code = db.added[0]
        self.assertEqual(code.code, "libertytalk0607")
        self.assertEqual(code.balance_cents, 10000)
        self.assertEqual(code.max_redemptions, 0)
        self.assertEqual(code.per_user_limit, 1)
        self.assertEqual(code.redemption_count, 0)
        self.assertEqual(code.note, "LibertyTalk 0607 campaign")

    async def test_admin_can_edit_campaign_code_settings(self):
        code = SimpleNamespace(
            id="rc_campaign",
            code="libertytalk0607",
            balance_cents=10000,
            status="active",
            max_redemptions=0,
            per_user_limit=1,
            redemption_count=2,
            note="old note",
        )
        db = _FakeDB([_FakeEntityResult(code)])
        payload = RedemptionCodeUpdateRequest(
            balance_cents=5000,
            max_redemptions=100,
            per_user_limit=2,
            note="edited campaign",
            status="active",
        )

        result = await admin_module.update_redemption_code("rc_campaign", payload, db)

        self.assertEqual(result["balance_cents"], 5000)
        self.assertEqual(result["max_redemptions"], 100)
        self.assertEqual(result["per_user_limit"], 2)
        self.assertEqual(result["redemption_count"], 2)
        self.assertEqual(result["note"], "edited campaign")
        self.assertEqual(code.balance_cents, 5000)
        self.assertEqual(code.max_redemptions, 100)
        self.assertEqual(code.per_user_limit, 2)
        self.assertEqual(code.note, "edited campaign")
        self.assertEqual(db.commits, 1)

    async def test_admin_cannot_set_total_limit_below_existing_redemptions(self):
        code = SimpleNamespace(
            id="rc_campaign",
            code="libertytalk0607",
            balance_cents=10000,
            status="active",
            max_redemptions=0,
            per_user_limit=1,
            redemption_count=5,
            note="",
        )
        db = _FakeDB([_FakeEntityResult(code)])
        payload = RedemptionCodeUpdateRequest(max_redemptions=3)

        with self.assertRaises(HTTPException) as ctx:
            await admin_module.update_redemption_code("rc_campaign", payload, db)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("max_redemptions", ctx.exception.detail)
        self.assertEqual(code.max_redemptions, 0)
        self.assertEqual(db.commits, 0)


if __name__ == "__main__":
    unittest.main()
