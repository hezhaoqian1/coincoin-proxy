import unittest
import json
from datetime import datetime
from types import SimpleNamespace

from app.billing import (
    BillingError,
    debit_usage_cents,
    get_available_balance_cents,
    serialize_billing_state,
)


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
    def __init__(self, execute_results=None):
        self.execute_results = list(execute_results or [])
        self.added = []
        self.queries = []

    async def execute(self, query):
        self.queries.append(query)
        if not self.execute_results:
            raise AssertionError("unexpected execute call")
        return self.execute_results.pop(0)

    def add(self, obj):
        self.added.append(obj)


class SubscriptionBillingTests(unittest.IsolatedAsyncioTestCase):
    def test_public_catalog_contains_only_three_permanent_usd_credit_products(self):
        now = datetime(2026, 5, 10, 12, 0, 0)
        sub = SimpleNamespace(
            id="sub_1",
            user_id="u_1",
            plan_id="monthly_basic",
            status="active",
            period_start=datetime(2026, 5, 1, 12, 0, 0),
            period_end=datetime(2026, 5, 31, 12, 0, 0),
            paid_until=datetime(2026, 5, 31, 12, 0, 0),
            quota_cents=40000,
            used_cents=40000,
        )
        user = SimpleNamespace(id="u_1", balance=0, referred_by=None, status="active")
        snapshot = serialize_billing_state(sub, [], user, now=now)
        products = snapshot["products"]
        credits = products.get("credits")

        self.assertIsNotNone(credits, "public catalog must expose permanent USD credits")
        self.assertEqual(
            [
                (
                    item["id"],
                    item["money"],
                    item["amount_fen"],
                    item["promised_credit_cents"],
                    item["purchase_action"],
                    item["catalog_version"],
                )
                for item in credits
            ],
            [
                ("credit_light", "59.90", 5990, 10000, "credit_purchase", "credit-v1"),
                ("credit_standard", "199.00", 19900, 40000, "credit_purchase", "credit-v1"),
                ("credit_pro", "399.00", 39900, 100000, "credit_purchase", "credit-v1"),
            ],
        )
        self.assertEqual(set(products), {"credits"})
        public_text = json.dumps(products, ensure_ascii=False)
        self.assertNotIn("月卡", public_text)
        self.assertNotIn("流量包", public_text)
        self.assertNotIn("upgrade", public_text)
        self.assertNotIn("renew", public_text)
        self.assertNotIn("reset", public_text)
        self.assertIn("美金额度", public_text)
        self.assertIn("$", public_text)

    def test_public_state_keeps_historical_active_subscription_metadata(self):
        now = datetime(2026, 5, 10, 12, 0, 0)
        sub = SimpleNamespace(
            id="sub_1",
            user_id="u_1",
            plan_id="monthly_basic",
            status="active",
            period_start=datetime(2026, 5, 1, 12, 0, 0),
            period_end=datetime(2026, 5, 31, 12, 0, 0),
            paid_until=datetime(2026, 5, 31, 12, 0, 0),
            quota_cents=40000,
            used_cents=2500,
        )
        user = SimpleNamespace(id="u_1", balance=0, referred_by=None, status="active")

        snapshot = serialize_billing_state(sub, [], user, now=now)

        self.assertTrue(snapshot["subscription"]["active"])
        self.assertEqual(snapshot["subscription"]["plan_id"], "monthly_basic")
        self.assertEqual(snapshot["subscription"]["plan_name"], "基础月卡")
        self.assertEqual(snapshot["subscription"]["remaining_cents"], 37500)

    async def test_debit_uses_subscription_then_pack_then_legacy_balance(self):
        now = datetime(2026, 5, 10, 12, 0, 0)
        sub = SimpleNamespace(
            id="sub_1",
            user_id="u_1",
            plan_id="monthly_light",
            status="active",
            period_start=datetime(2026, 5, 1, 12, 0, 0),
            period_end=datetime(2026, 5, 31, 12, 0, 0),
            paid_until=datetime(2026, 5, 31, 12, 0, 0),
            quota_cents=8000,
            used_cents=7500,
        )
        pack = SimpleNamespace(
            id="tp_1",
            user_id="u_1",
            product_id="addon_boost",
            status="active",
            remaining_cents=800,
            expires_at=datetime(2026, 8, 1, 0, 0, 0),
            created_at=now,
        )
        user = SimpleNamespace(id="u_1", balance=1000, referred_by=None, status="active")
        db = _FakeDB(execute_results=[_EntityResult(sub), _EntityResult([pack]), _EntityResult([])])

        result = await debit_usage_cents(db=db, user=user, cost_cents=1600, now=now)

        self.assertEqual(result["subscription_cents"], 500)
        self.assertEqual(result["traffic_pack_cents"], 800)
        self.assertEqual(result["legacy_cents"], 300)
        self.assertEqual(sub.used_cents, 8000)
        self.assertEqual(pack.remaining_cents, 0)
        self.assertEqual(pack.status, "depleted")
        self.assertEqual(user.balance, 700)

    async def test_debit_uses_subscription_pack_wallet_then_scalar_across_sources(self):
        now = datetime(2026, 7, 12, 12, 0, 0)
        sub = SimpleNamespace(
            id="sub_1",
            user_id="u_1",
            plan_id="monthly_light",
            status="active",
            period_start=datetime(2026, 7, 1, 12, 0, 0),
            period_end=datetime(2026, 7, 31, 12, 0, 0),
            paid_until=datetime(2026, 7, 31, 12, 0, 0),
            quota_cents=8000,
            used_cents=7900,
        )
        pack = SimpleNamespace(
            id="tp_1",
            user_id="u_1",
            product_id="addon_boost",
            status="active",
            remaining_cents=200,
            expires_at=datetime(2026, 8, 1),
            created_at=datetime(2026, 7, 2),
        )
        wallet_batch = SimpleNamespace(
            id="cb_1",
            user_id="u_1",
            status="active",
            original_cents=300,
            remaining_cents=300,
            created_at=datetime(2026, 7, 3),
        )
        user = SimpleNamespace(id="u_1", balance=400, referred_by=None, status="active")
        db = _FakeDB(
            execute_results=[
                _EntityResult(sub),
                _EntityResult([pack]),
                _EntityResult([wallet_batch]),
                _EntityResult([wallet_batch]),
            ]
        )

        result = await debit_usage_cents(
            db=db,
            user=user,
            cost_cents=700,
            allow_negative_legacy=False,
            now=now,
        )

        self.assertEqual(result["subscription_cents"], 100)
        self.assertEqual(result["traffic_pack_cents"], 200)
        self.assertEqual(result["credit_cents"], 300)
        self.assertEqual(result["legacy_cents"], 100)
        self.assertEqual(len(result["credit_allocations"]), 1)
        self.assertEqual(result["credit_allocations"][0]["credit_balance_id"], "cb_1")
        self.assertEqual(sub.used_cents, 8000)
        self.assertEqual(pack.remaining_cents, 0)
        self.assertEqual(wallet_batch.remaining_cents, 0)
        self.assertEqual(user.balance, 300)

    async def test_valid_pack_is_spendable_after_subscription_expiry(self):
        now = datetime(2026, 7, 12, 12, 0, 0)
        expired_sub = SimpleNamespace(
            id="sub_expired",
            user_id="u_1",
            plan_id="monthly_light",
            status="active",
            period_start=datetime(2026, 6, 1),
            period_end=datetime(2026, 7, 1),
            paid_until=datetime(2026, 7, 1),
            quota_cents=8000,
            used_cents=0,
        )
        pack = SimpleNamespace(
            id="tp_still_valid",
            user_id="u_1",
            product_id="addon_boost",
            status="active",
            remaining_cents=200,
            expires_at=datetime(2026, 8, 1),
            created_at=datetime(2026, 7, 2),
        )
        user = SimpleNamespace(id="u_1", balance=0, referred_by=None, status="active")
        db = _FakeDB(
            execute_results=[
                _EntityResult(expired_sub),
                _EntityResult([pack]),
                _EntityResult([]),
            ]
        )

        result = await debit_usage_cents(
            db=db,
            user=user,
            cost_cents=150,
            allow_negative_legacy=False,
            now=now,
        )

        self.assertEqual(result["subscription_cents"], 0)
        self.assertEqual(result["traffic_pack_cents"], 150)
        self.assertEqual(pack.remaining_cents, 50)
        self.assertEqual(user.balance, 0)
        pack_query = str(db.queries[1])
        self.assertIn(
            "ORDER BY coincoin_traffic_pack_balances.expires_at ASC, "
            "coincoin_traffic_pack_balances.created_at ASC, "
            "coincoin_traffic_pack_balances.id ASC",
            pack_query,
        )

    async def test_insufficient_total_does_not_partially_mutate_any_source(self):
        now = datetime(2026, 7, 12, 12, 0, 0)
        sub = SimpleNamespace(
            id="sub_1",
            user_id="u_1",
            plan_id="monthly_light",
            status="active",
            period_start=datetime(2026, 7, 1),
            period_end=datetime(2026, 7, 31),
            paid_until=datetime(2026, 7, 31),
            quota_cents=8000,
            used_cents=7900,
        )
        pack = SimpleNamespace(
            id="tp_1",
            user_id="u_1",
            product_id="addon_boost",
            status="active",
            remaining_cents=100,
            expires_at=datetime(2026, 8, 1),
            created_at=datetime(2026, 7, 2),
        )
        wallet_batch = SimpleNamespace(
            id="cb_1",
            user_id="u_1",
            status="active",
            original_cents=100,
            remaining_cents=100,
            created_at=datetime(2026, 7, 3),
        )
        user = SimpleNamespace(id="u_1", balance=-50, referred_by=None, status="active")
        db = _FakeDB(
            execute_results=[
                _EntityResult(sub),
                _EntityResult([pack]),
                _EntityResult([wallet_batch]),
            ]
        )

        with self.assertRaises(BillingError):
            await debit_usage_cents(
                db=db,
                user=user,
                cost_cents=251,
                allow_negative_legacy=False,
                now=now,
            )

        self.assertEqual(sub.used_cents, 7900)
        self.assertEqual(pack.remaining_cents, 100)
        self.assertEqual(pack.status, "active")
        self.assertEqual(wallet_batch.remaining_cents, 100)
        self.assertEqual(wallet_batch.status, "active")
        self.assertEqual(user.balance, -50)
        self.assertEqual(db.added, [])

    async def test_expired_subscription_insufficiency_does_not_normalize_or_dirty_fields(self):
        now = datetime(2026, 7, 12, 12, 0, 0)
        sub = SimpleNamespace(
            id="sub_expired",
            user_id="u_1",
            plan_id="monthly_light",
            status="active",
            period_start=datetime(2026, 6, 1),
            period_end=datetime(2026, 7, 1),
            paid_until=datetime(2026, 7, 1),
            quota_cents=8000,
            used_cents=123,
        )
        original = vars(sub).copy()
        user = SimpleNamespace(id="u_1", balance=0, referred_by=None, status="active")
        db = _FakeDB(
            execute_results=[_EntityResult(sub), _EntityResult([]), _EntityResult([])]
        )

        with self.assertRaises(BillingError):
            await debit_usage_cents(
                db=db,
                user=user,
                cost_cents=1,
                allow_negative_legacy=False,
                now=now,
            )

        self.assertEqual(vars(sub), original)
        self.assertEqual(db.added, [])

    async def test_rollover_subscription_insufficiency_does_not_reset_period_or_usage(self):
        now = datetime(2026, 7, 12, 12, 0, 0)
        sub = SimpleNamespace(
            id="sub_rollover",
            user_id="u_1",
            plan_id="monthly_light",
            status="active",
            period_start=datetime(2026, 6, 1),
            period_end=datetime(2026, 7, 1),
            paid_until=datetime(2026, 8, 1),
            quota_cents=100,
            used_cents=99,
        )
        original = vars(sub).copy()
        user = SimpleNamespace(id="u_1", balance=0, referred_by=None, status="active")
        db = _FakeDB(
            execute_results=[_EntityResult(sub), _EntityResult([]), _EntityResult([])]
        )

        with self.assertRaises(BillingError):
            await debit_usage_cents(
                db=db,
                user=user,
                cost_cents=101,
                allow_negative_legacy=False,
                now=now,
            )

        self.assertEqual(vars(sub), original)
        self.assertEqual(db.added, [])

    async def test_available_balance_and_serialization_include_wallet_and_scalar_debt(self):
        now = datetime(2026, 7, 12, 12, 0, 0)
        pack = SimpleNamespace(
            id="tp_1",
            user_id="u_1",
            product_id="addon_boost",
            status="active",
            original_cents=100,
            remaining_cents=100,
            expires_at=datetime(2026, 8, 1),
            created_at=datetime(2026, 7, 2),
        )
        wallet_batch = SimpleNamespace(id="cb_1", remaining_cents=300)
        user = SimpleNamespace(id="u_1", balance=-50, referred_by=None, status="active")
        db = _FakeDB(
            execute_results=[
                _EntityResult(None),
                _EntityResult([pack]),
                _EntityResult([wallet_batch]),
            ]
        )

        available = await get_available_balance_cents(db, user, now=now)
        serialized = serialize_billing_state(
            available["subscription"],
            available["traffic_packs"],
            user,
            now=now,
            credit_cents=available["credit_cents"],
        )

        self.assertEqual(available["traffic_pack_remaining_cents"], 100)
        self.assertEqual(available["credit_cents"], 300)
        self.assertEqual(available["legacy_balance_cents"], -50)
        self.assertEqual(available["available_cents"], 350)
        self.assertEqual(serialized["credit_balance"]["remaining_cents"], 300)
        self.assertEqual(serialized["available"]["remaining_cents"], 350)

    def test_serialize_billing_state_separates_active_and_historical_traffic_packs(self):
        now = datetime(2026, 5, 10, 12, 0, 0)
        sub = SimpleNamespace(
            id="sub_1",
            user_id="u_1",
            plan_id="monthly_basic",
            status="active",
            period_start=datetime(2026, 5, 1, 12, 0, 0),
            period_end=datetime(2026, 5, 31, 12, 0, 0),
            paid_until=datetime(2026, 5, 31, 12, 0, 0),
            quota_cents=40000,
            used_cents=1000,
        )
        active_pack = SimpleNamespace(
            id="tp_active",
            user_id="u_1",
            product_id="addon_boost",
            status="active",
            original_cents=30000,
            remaining_cents=12000,
            expires_at=datetime(2026, 8, 1, 0, 0, 0),
            created_at=datetime(2026, 5, 2, 0, 0, 0),
        )
        depleted_pack = SimpleNamespace(
            id="tp_old",
            user_id="u_1",
            product_id="addon_project",
            status="depleted",
            original_cents=100000,
            remaining_cents=0,
            expires_at=datetime(2026, 7, 1, 0, 0, 0),
            created_at=datetime(2026, 5, 1, 0, 0, 0),
        )
        user = SimpleNamespace(id="u_1", balance=500, referred_by=None, status="active")

        snapshot = serialize_billing_state(sub, [active_pack, depleted_pack], user, now=now)

        self.assertEqual(snapshot["traffic_packs"]["remaining_cents"], 12000)
        self.assertEqual([item["id"] for item in snapshot["traffic_packs"]["items"]], ["tp_active"])
        self.assertEqual(
            [item["id"] for item in snapshot["traffic_packs"]["all_items"]],
            ["tp_active", "tp_old"],
        )


if __name__ == "__main__":
    unittest.main()
