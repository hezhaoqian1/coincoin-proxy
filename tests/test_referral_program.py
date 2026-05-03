import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import app.openai_compat as openai_module
import app.referral as referral_module


class _ScalarOneOrNoneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        if self._value is None:
            raise AssertionError("expected entity, got None")
        return self._value


class _ScalarValueResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _ScalarsCollection:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _ScalarsCollection(self._rows)


class _FakeDB:
    def __init__(self, execute_results=None):
        self.execute_results = list(execute_results or [])
        self.added = []

    async def execute(self, _query):
        if not self.execute_results:
            raise AssertionError("unexpected execute call")
        return self.execute_results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        return None


class ReferralProgramTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._settings = {
            "referral_signup_bonus_cents": referral_module.settings.referral_signup_bonus_cents,
            "referral_signup_referrer_bonus_cents": referral_module.settings.referral_signup_referrer_bonus_cents,
            "referral_first_usage_referrer_bonus_cents": referral_module.settings.referral_first_usage_referrer_bonus_cents,
            "referral_new_user_bonus_cents": referral_module.settings.referral_new_user_bonus_cents,
            "referral_commission_rate": referral_module.settings.referral_commission_rate,
            "referral_max_rewards_per_user": referral_module.settings.referral_max_rewards_per_user,
            "referral_reward_cap_cents": referral_module.settings.referral_reward_cap_cents,
        }
        referral_module.settings.referral_signup_bonus_cents = 1000
        referral_module.settings.referral_signup_referrer_bonus_cents = 500
        referral_module.settings.referral_first_usage_referrer_bonus_cents = 500
        referral_module.settings.referral_new_user_bonus_cents = 2000
        referral_module.settings.referral_commission_rate = 0.20
        referral_module.settings.referral_max_rewards_per_user = 0
        referral_module.settings.referral_reward_cap_cents = 0

    async def asyncTearDown(self):
        for key, value in self._settings.items():
            setattr(referral_module.settings, key, value)

    async def test_signup_rewards_credit_both_sides_once(self):
        referrer = SimpleNamespace(id="u_ref", status="active", balance=100)
        invited = SimpleNamespace(id="u_inv", referred_by="u_ref", balance=0)
        db = _FakeDB(
            execute_results=[
                _ScalarOneOrNoneResult(referrer),
                _ScalarOneOrNoneResult(None),
                _ScalarOneOrNoneResult(None),
            ]
        )

        with patch.object(referral_module, "ensure_finance_summary_initialized", AsyncMock()), patch.object(
            referral_module, "increment_finance_summary", AsyncMock()
        ):
            total = await referral_module.process_signup_referral_rewards(invited, db)

        self.assertEqual(total, 1500)
        self.assertEqual(invited.balance, 1000)
        self.assertEqual(referrer.balance, 600)
        self.assertEqual(
            [row.reward_type for row in db.added],
            [
                referral_module.REWARD_SIGNUP_INVITED,
                referral_module.REWARD_SIGNUP_REFERRER,
            ],
        )
        self.assertEqual(db.added[0].recipient_id, "u_inv")
        self.assertEqual(db.added[1].recipient_id, "u_ref")

    async def test_first_usage_rewards_referrer(self):
        invited = SimpleNamespace(id="u_inv", referred_by="u_ref")
        referrer = SimpleNamespace(id="u_ref", status="active", balance=0)
        db = _FakeDB(
            execute_results=[
                _ScalarOneOrNoneResult(invited),
                _ScalarOneOrNoneResult(referrer),
                _ScalarOneOrNoneResult(None),
            ]
        )

        with patch.object(referral_module, "ensure_finance_summary_initialized", AsyncMock()), patch.object(
            referral_module, "increment_finance_summary", AsyncMock()
        ):
            total = await referral_module.process_first_usage_referral_reward("u_inv", db)

        self.assertEqual(total, 500)
        self.assertEqual(referrer.balance, 500)
        self.assertEqual(db.added[0].reward_type, referral_module.REWARD_FIRST_USAGE_REFERRER)
        self.assertEqual(db.added[0].recipient_id, "u_ref")

    async def test_purchase_rewards_invited_first_purchase_and_referrer_commission(self):
        invited = SimpleNamespace(id="u_inv", referred_by="u_ref", balance=4999)
        referrer = SimpleNamespace(id="u_ref", status="active", balance=0)
        db = _FakeDB(
            execute_results=[
                _ScalarOneOrNoneResult(referrer),
                _ScalarOneOrNoneResult(None),
                _ScalarOneOrNoneResult(None),
                _ScalarOneOrNoneResult(None),
                _ScalarValueResult(0),
                _ScalarValueResult(0),
                _ScalarOneOrNoneResult(None),
            ]
        )

        with patch.object(referral_module, "ensure_finance_summary_initialized", AsyncMock()), patch.object(
            referral_module, "increment_finance_summary", AsyncMock()
        ):
            total = await referral_module.process_referral_reward(invited, 4999, "CC_1", db)

        self.assertEqual(total, 2999)
        self.assertEqual(invited.balance, 6999)
        self.assertEqual(referrer.balance, 999)
        self.assertEqual(
            [row.reward_type for row in db.added],
            [
                referral_module.REWARD_FIRST_PURCHASE_INVITED,
                referral_module.REWARD_PURCHASE_COMMISSION,
            ],
        )

    def test_build_referral_record_splits_friend_and_referrer_rewards(self):
        referred = SimpleNamespace(
            id="u_inv",
            username="alice",
            email="alice@example.com",
            referred_by="u_ref",
            created_at=datetime(2026, 5, 3, 1, 2, 3),
        )
        rewards = [
            SimpleNamespace(
                referrer_id="u_ref",
                referred_id="u_inv",
                recipient_id="u_inv",
                reward_type=referral_module.REWARD_SIGNUP_INVITED,
                reward_cents=1000,
                order_amount_cents=0,
                created_at=datetime(2026, 5, 3, 1, 3, 0),
            ),
            SimpleNamespace(
                referrer_id="u_ref",
                referred_id="u_inv",
                recipient_id="u_ref",
                reward_type=referral_module.REWARD_FIRST_USAGE_REFERRER,
                reward_cents=500,
                order_amount_cents=0,
                created_at=datetime(2026, 5, 3, 1, 4, 0),
            ),
        ]

        record = referral_module.build_referral_record(referred, rewards)

        self.assertEqual(record["referrer_reward_cents"], 500)
        self.assertEqual(record["referred_reward_cents"], 1000)
        self.assertEqual(record["status"], "已开始使用")
        self.assertEqual(record["next_step"], "等待首次充值")

    async def test_referral_info_api_returns_records_and_reward_totals(self):
        referrer = SimpleNamespace(id="u_ref", referral_code="BIRD2026")
        invited = SimpleNamespace(
            id="u_inv",
            username="alice",
            email="alice@example.com",
            referred_by="u_ref",
            created_at=datetime(2026, 5, 3, 1, 2, 3),
        )
        rewards = [
            SimpleNamespace(
                referrer_id="u_ref",
                referred_id="u_inv",
                recipient_id="u_ref",
                reward_type=referral_module.REWARD_SIGNUP_REFERRER,
                order_no="",
                order_amount_cents=0,
                reward_cents=500,
                created_at=datetime(2026, 5, 3, 1, 3, 0),
            ),
            SimpleNamespace(
                referrer_id="u_ref",
                referred_id="u_inv",
                recipient_id="u_inv",
                reward_type=referral_module.REWARD_SIGNUP_INVITED,
                order_no="",
                order_amount_cents=0,
                reward_cents=1000,
                created_at=datetime(2026, 5, 3, 1, 4, 0),
            ),
            SimpleNamespace(
                referrer_id="u_ref",
                referred_id="u_inv",
                recipient_id="u_ref",
                reward_type=referral_module.REWARD_FIRST_USAGE_REFERRER,
                order_no="",
                order_amount_cents=0,
                reward_cents=500,
                created_at=datetime(2026, 5, 3, 1, 5, 0),
            ),
        ]
        db = _FakeDB(
            execute_results=[
                _ScalarOneOrNoneResult(referrer),
                _ScalarsResult([invited]),
                _ScalarValueResult(1000),
                _ScalarsResult(rewards),
            ]
        )

        with patch.object(openai_module, "authenticate_user", AsyncMock(return_value=referrer)):
            payload = await openai_module.get_referral_info(SimpleNamespace(), db)

        self.assertEqual(payload["referral_code"], "BIRD2026")
        self.assertEqual(payload["invite_url_path"], "/register?ref=BIRD2026")
        self.assertEqual(payload["invited_count"], 1)
        self.assertEqual(payload["total_reward_cents"], 1000)
        self.assertEqual(payload["friend_reward_cents"], 1000)
        self.assertEqual(payload["pending_count"], 1)
        self.assertEqual(payload["records"][0]["username"], "alice")
        self.assertEqual(payload["records"][0]["status"], "已开始使用")
        self.assertEqual(payload["records"][0]["next_step"], "等待首次充值")
        self.assertEqual(payload["records"][0]["referrer_reward_cents"], 1000)
        self.assertEqual(payload["records"][0]["referred_reward_cents"], 1000)

    async def test_referral_code_update_rejects_reserved_and_duplicate_codes(self):
        cached_user = SimpleNamespace(id="u_ref")
        db = _FakeDB()

        with patch.object(openai_module, "authenticate_user", AsyncMock(return_value=cached_user)):
            with self.assertRaises(HTTPException) as reserved:
                await openai_module.update_referral_code(
                    openai_module.ReferralCodeUpdateRequest(referral_code="birdsync"),
                    SimpleNamespace(),
                    db,
                )
        self.assertEqual(reserved.exception.status_code, 400)

        duplicate = SimpleNamespace(id="u_other")
        db = _FakeDB(execute_results=[_ScalarOneOrNoneResult(duplicate)])
        with patch.object(openai_module, "authenticate_user", AsyncMock(return_value=cached_user)):
            with self.assertRaises(HTTPException) as conflict:
                await openai_module.update_referral_code(
                    openai_module.ReferralCodeUpdateRequest(referral_code="alice2026"),
                    SimpleNamespace(),
                    db,
                )
        self.assertEqual(conflict.exception.status_code, 409)

    async def test_referral_code_update_accepts_available_code(self):
        cached_user = SimpleNamespace(id="u_ref")
        user = SimpleNamespace(id="u_ref", referral_code="OLD2026")
        db = _FakeDB(
            execute_results=[
                _ScalarOneOrNoneResult(None),
                _ScalarOneOrNoneResult(user),
            ]
        )

        with patch.object(openai_module, "authenticate_user", AsyncMock(return_value=cached_user)):
            payload = await openai_module.update_referral_code(
                openai_module.ReferralCodeUpdateRequest(referral_code="new2026"),
                SimpleNamespace(),
                db,
            )

        self.assertEqual(user.referral_code, "NEW2026")
        self.assertEqual(payload, {"referral_code": "NEW2026", "invite_url_path": "/register?ref=NEW2026"})


if __name__ == "__main__":
    unittest.main()
