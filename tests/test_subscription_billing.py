import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.billing import (
    ADDONS_BY_ID,
    MONTHLY_BY_ID,
    BillingError,
    active_subscription,
    apply_payment_product,
    debit_usage_cents,
    serialize_billing_state,
    validate_product_purchase,
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

    async def execute(self, _query):
        if not self.execute_results:
            raise AssertionError("unexpected execute call")
        return self.execute_results.pop(0)

    def add(self, obj):
        self.added.append(obj)


class SubscriptionBillingTests(unittest.IsolatedAsyncioTestCase):
    async def test_monthly_purchase_starts_subscription(self):
        now = datetime(2026, 5, 1, 12, 0, 0)
        user = SimpleNamespace(id="u_1", balance=0, referred_by=None, status="active")
        db = _FakeDB(execute_results=[_EntityResult(None)])

        result = await apply_payment_product(
            user=user,
            product=MONTHLY_BY_ID["monthly_basic"],
            order_no="CC_1",
            db=db,
            now=now,
        )

        sub = result["subscription"]
        self.assertEqual(result["billing_action"], "subscription_start")
        self.assertEqual(sub.plan_id, "monthly_basic")
        self.assertEqual(sub.quota_cents, 38000)
        self.assertEqual(sub.used_cents, 0)
        self.assertEqual(sub.period_start, now)
        self.assertEqual(sub.period_end, now + timedelta(days=30))
        self.assertTrue(active_subscription(sub, now))
        self.assertEqual(user.balance, 0)

    async def test_same_tier_purchase_renews_without_resetting_usage(self):
        now = datetime(2026, 5, 10, 12, 0, 0)
        sub = SimpleNamespace(
            id="sub_1",
            user_id="u_1",
            plan_id="monthly_basic",
            status="active",
            period_start=datetime(2026, 5, 1, 12, 0, 0),
            period_end=datetime(2026, 5, 31, 12, 0, 0),
            paid_until=datetime(2026, 5, 31, 12, 0, 0),
            quota_cents=38000,
            used_cents=12000,
        )
        user = SimpleNamespace(id="u_1", balance=0, referred_by=None, status="active")
        db = _FakeDB(execute_results=[_EntityResult(sub)])

        result = await apply_payment_product(
            user=user,
            product=MONTHLY_BY_ID["monthly_basic"],
            order_no="CC_2",
            db=db,
            now=now,
        )

        self.assertEqual(result["billing_action"], "subscription_renew")
        self.assertEqual(sub.used_cents, 12000)
        self.assertEqual(sub.quota_cents, 38000)
        self.assertEqual(sub.paid_until, datetime(2026, 6, 30, 12, 0, 0))
        self.assertEqual(sub.period_end, datetime(2026, 5, 31, 12, 0, 0))

    async def test_lower_tier_purchase_is_rejected(self):
        now = datetime(2026, 5, 10, 12, 0, 0)
        sub = SimpleNamespace(
            id="sub_1",
            user_id="u_1",
            plan_id="monthly_flagship",
            status="active",
            period_start=datetime(2026, 5, 1, 12, 0, 0),
            period_end=datetime(2026, 5, 31, 12, 0, 0),
            paid_until=datetime(2026, 5, 31, 12, 0, 0),
            quota_cents=100000,
            used_cents=0,
        )
        db = _FakeDB(execute_results=[_EntityResult(sub)])

        with self.assertRaises(BillingError):
            await validate_product_purchase(
                user_id="u_1",
                product=MONTHLY_BY_ID["monthly_light"],
                money="29.90",
                db=db,
                now=now,
            )

    async def test_upgrade_quote_uses_remaining_period_proration(self):
        now = datetime(2026, 5, 11, 12, 0, 0)
        sub = SimpleNamespace(
            id="sub_1",
            user_id="u_1",
            plan_id="monthly_light",
            status="active",
            period_start=datetime(2026, 5, 1, 12, 0, 0),
            period_end=datetime(2026, 5, 31, 12, 0, 0),
            paid_until=datetime(2026, 5, 31, 12, 0, 0),
            quota_cents=7500,
            used_cents=2500,
        )
        user = SimpleNamespace(id="u_1", balance=0, referred_by=None, status="active")
        snapshot = serialize_billing_state(sub, [], user, now=now)
        basic = next(item for item in snapshot["products"]["monthly"] if item["id"] == "monthly_basic")

        self.assertEqual(basic["purchase_action"], "upgrade")
        self.assertEqual(basic["pay_money"], "66.07")

        db = _FakeDB(execute_results=[_EntityResult(sub)])
        normalized_money = await validate_product_purchase(
            user_id="u_1",
            product=MONTHLY_BY_ID["monthly_basic"],
            money="66.07",
            db=db,
            now=now,
        )

        self.assertEqual(normalized_money, "66.07")

    async def test_addon_requires_active_subscription_and_grants_180_days(self):
        now = datetime(2026, 5, 10, 12, 0, 0)
        sub = SimpleNamespace(
            id="sub_1",
            user_id="u_1",
            plan_id="monthly_flagship",
            status="active",
            period_start=datetime(2026, 5, 1, 12, 0, 0),
            period_end=datetime(2026, 5, 31, 12, 0, 0),
            paid_until=datetime(2026, 5, 31, 12, 0, 0),
            quota_cents=100000,
            used_cents=0,
        )
        user = SimpleNamespace(id="u_1", balance=0, referred_by=None, status="active")
        db = _FakeDB(execute_results=[_EntityResult(sub)])

        result = await apply_payment_product(
            user=user,
            product=ADDONS_BY_ID["addon_ultra"],
            order_no="CC_3",
            db=db,
            now=now,
        )

        pack = result["traffic_pack"]
        self.assertEqual(result["billing_action"], "traffic_pack_grant")
        self.assertEqual(pack.remaining_cents, 200000)
        self.assertEqual(pack.expires_at, now + timedelta(days=180))

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
            quota_cents=7500,
            used_cents=7000,
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
        db = _FakeDB(execute_results=[_EntityResult(sub), _EntityResult([pack])])

        result = await debit_usage_cents(db=db, user=user, cost_cents=1600, now=now)

        self.assertEqual(result["subscription_cents"], 500)
        self.assertEqual(result["traffic_pack_cents"], 800)
        self.assertEqual(result["legacy_cents"], 300)
        self.assertEqual(sub.used_cents, 7500)
        self.assertEqual(pack.remaining_cents, 0)
        self.assertEqual(pack.status, "depleted")
        self.assertEqual(user.balance, 700)

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
            quota_cents=38000,
            used_cents=1000,
        )
        active_pack = SimpleNamespace(
            id="tp_active",
            user_id="u_1",
            product_id="addon_boost",
            status="active",
            original_cents=28000,
            remaining_cents=12000,
            expires_at=datetime(2026, 8, 1, 0, 0, 0),
            created_at=datetime(2026, 5, 2, 0, 0, 0),
        )
        depleted_pack = SimpleNamespace(
            id="tp_old",
            user_id="u_1",
            product_id="addon_project",
            status="depleted",
            original_cents=110000,
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
