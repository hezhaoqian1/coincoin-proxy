import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.main import app
import app.admin as admin_module
import app.epay as epay_module
import app.payment as payment_module
import app.proxy as proxy_module
import app.webhook as webhook_module
import app.openai_compat as openai_module


class _FakeAllResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeEntityResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        if self._value is None:
            raise AssertionError("expected entity, got None")
        return self._value


class _FakeScalarOneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeScalarsCollection:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeScalarsCollection(self._rows)


class _FakeDB:
    def __init__(self, *, execute_results=None, scalar_results=None):
        self._execute_results = list(execute_results or [])
        self._scalar_results = list(scalar_results or [])
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, _query):
        if not self._execute_results:
            raise AssertionError("unexpected execute call")
        return self._execute_results.pop(0)

    async def scalar(self, _query):
        if not self._scalar_results:
            raise AssertionError("unexpected scalar call")
        return self._scalar_results.pop(0)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    def add(self, _obj):
        return None


class AdminUsageFieldTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        app.dependency_overrides.pop(admin_module.get_db, None)
        app.dependency_overrides.pop(admin_module.admin_guard, None)
        app.dependency_overrides.pop(payment_module.get_db, None)
        app.dependency_overrides.pop(webhook_module.get_db, None)
        payment_module.settings.epay_api_url = ""
        payment_module.settings.epay_pid = ""
        payment_module.settings.epay_key = ""
        payment_module.settings.epay_site_name = "CoinCoin"
        payment_module.settings.self_base_url = ""
        epay_module.settings.epay_api_url = ""
        epay_module.settings.epay_pid = ""
        epay_module.settings.epay_key = ""
        epay_module.settings.epay_site_name = "CoinCoin"

    async def test_daily_usage_exposes_image_totals(self) -> None:
        usage = SimpleNamespace(
            user_id="u_1",
            day=date(2026, 3, 25),
            tokens_total=12345,
            input_tokens=10000,
            output_tokens=2345,
            images_total=4,
            cost_cents=88,
            requests_total=7,
        )
        user = SimpleNamespace(username="alice", external_id="ext_alice")
        fake_db = _FakeDB(execute_results=[_FakeAllResult([(usage, user)])])

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/admin/usage/daily")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload[0]["images_total"], 4)
        self.assertEqual(payload[0]["tokens_total"], 12345)
        self.assertEqual(payload[0]["cost_usd"], 0.88)

    async def test_request_logs_expose_provider_alias_and_usage_units(self) -> None:
        log = SimpleNamespace(
            created_at=datetime(2026, 3, 25, 12, 34, 56),
            api_key_id="k_img",
            endpoint="images/generations",
            model="vertex-gemini-3.1-flash-image-preview",
            input_tokens=0,
            output_tokens=0,
            cached_tokens=0,
            image_count=2,
            provider_model="gemini-3.1-flash-image-preview",
            customer_model_alias="gemini-image",
            usage_unit_type="images",
            usage_unit_count=2,
            billable_sku="gemini-image",
            upstream_request_id="req_img_123",
            cost_cents=14,
            duration_ms=2100,
            status_code=200,
            route_reason="catalog:gemini-image:gateway",
        )
        fake_db = _FakeDB(
            execute_results=[
                _FakeScalarResult(1),
                _FakeScalarsResult([log]),
            ]
        )

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/admin/users/u_1/request-logs")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        item = payload["data"][0]
        self.assertEqual(item["model"], "gemini-image")
        self.assertEqual(item["provider_model"], "gemini-3.1-flash-image-preview")
        self.assertEqual(item["customer_model_alias"], "gemini-image")
        self.assertEqual(item["usage_unit_type"], "images")
        self.assertEqual(item["usage_unit_count"], 2)
        self.assertEqual(item["image_count"], 2)
        self.assertEqual(item["billable_sku"], "gemini-image")
        self.assertEqual(item["upstream_request_id"], "req_img_123")

    async def test_user_usage_can_filter_by_api_key(self) -> None:
        user = SimpleNamespace(id="u_1")
        log = SimpleNamespace(
            created_at=datetime(2026, 5, 1, 18, 23, 19),
            api_key_id="k_selected",
            endpoint="responses",
            model="gpt-5.4",
            input_tokens=10,
            output_tokens=5,
            cached_tokens=0,
            image_count=0,
            provider_model="gpt-5.4",
            customer_model_alias="gpt-5.4",
            usage_unit_type="tokens",
            usage_unit_count=15,
            billable_sku="gpt-5.4",
            cost_cents=1,
            duration_ms=1200,
            status_code=200,
            route_reason="catalog:gpt-5.4",
        )
        fake_db = _FakeDB(
            execute_results=[
                _FakeScalarResult(1),
                _FakeScalarsResult([log]),
            ]
        )

        with patch.object(openai_module, "authenticate_user", AsyncMock(return_value=user)):
            payload = await openai_module.get_usage(
                SimpleNamespace(),
                fake_db,
                api_key_id="k_selected",
            )

        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["data"][0]["api_key_id"], "k_selected")

    async def test_summary_metrics_expose_images_today(self) -> None:
        fake_db = _FakeDB(scalar_results=[12, 10, 987654, 45, 6, 999, 321])

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/admin/metrics/summary")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["total_users"], 12)
        self.assertEqual(payload["active_users"], 10)
        self.assertEqual(payload["total_tokens"], 987654)
        self.assertEqual(payload["total_requests_today"], 45)
        self.assertEqual(payload["total_images_today"], 6)
        self.assertEqual(payload["paid_today_cents"], 999)
        self.assertEqual(payload["consumed_today_cents"], 321)

    async def test_manual_payment_confirm_credits_pending_order_from_proof_url(self) -> None:
        admin_module._settings.epay_api_url = "https://code.nxslq.top/"
        admin_module._settings.epay_pid = "177938431"
        admin_module._settings.epay_key = "j9J4loEx5Qy"
        order = SimpleNamespace(
            order_no="CC_test_order",
            user_id="u_1",
            amount_rmb="9.90",
            status="pending",
            add_balance_cents=4999,
            trade_no=None,
            confirmed_at=None,
        )
        user = SimpleNamespace(
            id="u_1",
            balance=500,
            referred_by=None,
            status="active",
        )
        fake_db = _FakeDB(
            execute_results=[
                _FakeEntityResult(order),
                _FakeEntityResult(order),
                _FakeEntityResult(user),
                _FakeEntityResult(None),
                _FakeScalarOneResult(None),
                _FakeAllResult([]),
                _FakeAllResult([]),
                _FakeAllResult([]),
                _FakeAllResult([]),
                _FakeEntityResult(None),
            ]
        )

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/admin/payment-orders/CC_test_order/manual-confirm",
                json={
                    "proof_url": "https://bird-alipay.up.railway.app/pay/return"
                    "?pid=177938431&trade_no=2026032622080275954&out_trade_no=CC_test_order"
                    "&type=alipay&name=%E4%BD%93%E9%AA%8C%E5%8C%85&money=9.90&trade_status=TRADE_SUCCESS"
                    "&sign=f1b31796bddaf4e9e156657dba3a0159&sign_type=MD5"
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["status"], "confirmed")
        self.assertEqual(payload["trade_no"], "2026032622080275954")
        self.assertEqual(payload["added_cents"], 4999)
        self.assertEqual(user.balance, 5499)
        self.assertEqual(order.status, "confirmed")
        self.assertEqual(order.trade_no, "2026032622080275954")
        self.assertEqual(fake_db.commits, 1)
        self.assertIsNotNone(order.confirmed_at)

    async def test_manual_payment_confirm_rejects_proof_for_another_order(self) -> None:
        admin_module._settings.epay_api_url = "https://code.nxslq.top/"
        admin_module._settings.epay_pid = "177938431"
        admin_module._settings.epay_key = "j9J4loEx5Qy"
        order = SimpleNamespace(
            order_no="CC_test_order",
            user_id="u_1",
            amount_rmb="9.90",
            status="pending",
            add_balance_cents=4999,
            trade_no=None,
            confirmed_at=None,
        )
        fake_db = _FakeDB(execute_results=[_FakeEntityResult(order)])

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/admin/payment-orders/CC_test_order/manual-confirm",
                json={
                    "proof_url": "https://bird-alipay.up.railway.app/pay/return"
                    "?pid=177938431&trade_no=2026032622080275954&out_trade_no=CC_other_order"
                    "&type=alipay&name=%E4%BD%93%E9%AA%8C%E5%8C%85&money=9.90&trade_status=TRADE_SUCCESS"
                    "&sign=ecc2773589dd2c440e03d798adc4b2f9&sign_type=MD5"
                },
            )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("does not match this order", response.text)
        self.assertEqual(fake_db.commits, 0)

    async def test_create_order_builds_direct_epay_submit_url(self) -> None:
        payment_module.settings.epay_api_url = "https://code.nxslq.top/"
        payment_module.settings.epay_pid = "177938431"
        payment_module.settings.epay_key = "j9J4loEx5Qy"
        payment_module.settings.epay_site_name = "Clawfather"
        payment_module.settings.self_base_url = "https://bird-alipay.up.railway.app"

        user = SimpleNamespace(id="u_1")
        fake_db = _FakeDB()

        async def fake_get_db():
            yield fake_db

        async def fake_authenticate_user(_request, _db):
            return user

        async def fake_allow(_key, _limit):
            return True

        original_authenticate_user = payment_module.authenticate_user
        original_allow = payment_module.rate_limiter.allow
        payment_module.authenticate_user = fake_authenticate_user
        payment_module.rate_limiter.allow = fake_allow
        app.dependency_overrides[payment_module.get_db] = fake_get_db

        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/v1/orders/create",
                    json={"name": "体验包 套餐", "money": "9.90", "pay_type": "alipay"},
                    headers={"Authorization": "Bearer sk_cc_test"},
                )
        finally:
            payment_module.authenticate_user = original_authenticate_user
            payment_module.rate_limiter.allow = original_allow

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["pay_url"].startswith("https://code.nxslq.top/submit.php?"))
        self.assertIn("notify_url=https%3A%2F%2Fbird-alipay.up.railway.app%2Fwebhook%2Fpay-notify", payload["pay_url"])
        self.assertIn("return_url=https%3A%2F%2Fbird-alipay.up.railway.app%2Fpay%2Freturn%3Forder_no%3D", payload["pay_url"])
        self.assertIn("sign=", payload["pay_url"])
        self.assertEqual(payload["expected_cents"], 4999)
        self.assertEqual(fake_db.commits, 1)

    async def test_confirm_order_accepts_signed_proof_url(self) -> None:
        payment_module.settings.epay_api_url = "https://code.nxslq.top/"
        payment_module.settings.epay_pid = "177938431"
        payment_module.settings.epay_key = "j9J4loEx5Qy"

        user = SimpleNamespace(id="u_1", balance=500, referred_by=None, status="active")
        order = SimpleNamespace(
            order_no="CC_test_order",
            user_id="u_1",
            amount_rmb="9.90",
            status="pending",
            add_balance_cents=4999,
            trade_no=None,
            confirmed_at=None,
        )
        fake_db = _FakeDB(
            execute_results=[
                _FakeEntityResult(order),
                _FakeEntityResult(order),
                _FakeEntityResult(user),
                _FakeEntityResult(None),
                _FakeScalarOneResult(None),
                _FakeAllResult([]),
                _FakeAllResult([]),
                _FakeAllResult([]),
                _FakeAllResult([]),
                _FakeEntityResult(None),
            ]
        )

        async def fake_get_db():
            yield fake_db

        async def fake_authenticate_user(_request, _db):
            return user

        async def fake_allow(_key, _limit):
            return True

        original_authenticate_user = payment_module.authenticate_user
        original_allow = payment_module.rate_limiter.allow
        payment_module.authenticate_user = fake_authenticate_user
        payment_module.rate_limiter.allow = fake_allow
        app.dependency_overrides[payment_module.get_db] = fake_get_db

        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/v1/orders/confirm",
                    json={
                        "order_no": "CC_test_order",
                        "proof_url": "https://bird-alipay.up.railway.app/pay/return?order_no=CC_test_order"
                        "&pid=177938431&trade_no=2026032622080275954&out_trade_no=CC_test_order"
                        "&type=alipay&name=%E4%BD%93%E9%AA%8C%E5%8C%85&money=9.90&trade_status=TRADE_SUCCESS"
                        "&sign=f1b31796bddaf4e9e156657dba3a0159&sign_type=MD5",
                    },
                    headers={"Authorization": "Bearer sk_cc_test"},
                )
        finally:
            payment_module.authenticate_user = original_authenticate_user
            payment_module.rate_limiter.allow = original_allow

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["added_cents"], 4999)
        self.assertEqual(user.balance, 5499)
        self.assertEqual(order.trade_no, "2026032622080275954")
        self.assertEqual(fake_db.commits, 1)

    async def test_confirm_order_keeps_stored_pending_balance_quote(self) -> None:
        payment_module.settings.epay_api_url = "https://code.nxslq.top/"
        payment_module.settings.epay_pid = "177938431"
        payment_module.settings.epay_key = "j9J4loEx5Qy"

        user = SimpleNamespace(id="u_1", balance=500, referred_by=None, status="active")
        order = SimpleNamespace(
            order_no="CC_test_order",
            user_id="u_1",
            amount_rmb="9.90",
            status="pending",
            add_balance_cents=4321,
            trade_no=None,
            confirmed_at=None,
        )
        fake_db = _FakeDB(
            execute_results=[
                _FakeEntityResult(order),
                _FakeEntityResult(order),
                _FakeEntityResult(user),
                _FakeEntityResult(None),
                _FakeScalarOneResult(None),
                _FakeAllResult([]),
                _FakeAllResult([]),
                _FakeAllResult([]),
                _FakeAllResult([]),
                _FakeEntityResult(None),
            ]
        )

        async def fake_get_db():
            yield fake_db

        async def fake_authenticate_user(_request, _db):
            return user

        async def fake_allow(_key, _limit):
            return True

        original_authenticate_user = payment_module.authenticate_user
        original_allow = payment_module.rate_limiter.allow
        payment_module.authenticate_user = fake_authenticate_user
        payment_module.rate_limiter.allow = fake_allow
        app.dependency_overrides[payment_module.get_db] = fake_get_db

        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/v1/orders/confirm",
                    json={
                        "order_no": "CC_test_order",
                        "proof_url": "https://bird-alipay.up.railway.app/pay/return?order_no=CC_test_order"
                        "&pid=177938431&trade_no=2026032622080275954&out_trade_no=CC_test_order"
                        "&type=alipay&name=%E4%BD%93%E9%AA%8C%E5%8C%85&money=9.90&trade_status=TRADE_SUCCESS"
                        "&sign=f1b31796bddaf4e9e156657dba3a0159&sign_type=MD5",
                    },
                    headers={"Authorization": "Bearer sk_cc_test"},
                )
        finally:
            payment_module.authenticate_user = original_authenticate_user
            payment_module.rate_limiter.allow = original_allow

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["added_cents"], 4321)
        self.assertEqual(user.balance, 4821)
        self.assertEqual(order.add_balance_cents, 4321)
        self.assertEqual(fake_db.commits, 1)

    async def test_pay_notify_confirms_order_from_signed_callback(self) -> None:
        webhook_module.settings.epay_api_url = "https://code.nxslq.top/"
        webhook_module.settings.epay_pid = "177938431"
        webhook_module.settings.epay_key = "j9J4loEx5Qy"

        order = SimpleNamespace(
            order_no="CC_test_order",
            user_id="u_1",
            amount_rmb="9.90",
            status="pending",
            add_balance_cents=4999,
            trade_no=None,
            confirmed_at=None,
        )
        user = SimpleNamespace(
            id="u_1",
            balance=500,
            referred_by=None,
            status="active",
        )
        fake_db = _FakeDB(
            execute_results=[
                _FakeEntityResult(order),
                _FakeEntityResult(user),
                _FakeEntityResult(None),
                _FakeScalarOneResult(None),
                _FakeAllResult([]),
                _FakeAllResult([]),
                _FakeAllResult([]),
                _FakeAllResult([]),
                _FakeEntityResult(None),
            ]
        )

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[webhook_module.get_db] = fake_get_db

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get(
                "/webhook/pay-notify",
                params={
                    "pid": "177938431",
                    "trade_no": "2026032622080275954",
                    "out_trade_no": "CC_test_order",
                    "type": "alipay",
                    "name": "体验包",
                    "money": "9.90",
                    "trade_status": "TRADE_SUCCESS",
                    "sign": "f1b31796bddaf4e9e156657dba3a0159",
                    "sign_type": "MD5",
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.text, "success")
        self.assertEqual(order.status, "confirmed")
        self.assertEqual(user.balance, 5499)
        self.assertEqual(fake_db.commits, 1)

    async def test_user_detail_exposes_finance_summary(self) -> None:
        user = SimpleNamespace(
            id="u_1",
            username="alice",
            external_id="ext_alice",
            status="active",
            balance=4321,
            token_limit=None,
            token_used=123,
            input_tokens_used=100,
            output_tokens_used=23,
            request_limit_per_minute=None,
            request_limit_per_day=None,
            created_at=datetime(2026, 3, 25, 10, 0, 0),
            updated_at=datetime(2026, 3, 25, 10, 0, 0),
        )
        station_link = SimpleNamespace(
            id="sclink_1",
            status="active",
            created_at=datetime(2026, 3, 25, 9, 30, 0),
        )
        station = SimpleNamespace(
            id="st_1",
            display_name="Alpha Station",
            slug="alpha-station",
            owner_user_id="u_owner",
            status="active",
        )
        finance_summary = SimpleNamespace(
            user_id="u_1",
            initialized_from_history=1,
            total_paid_rmb_cents=990,
            total_paid_balance_cents=4999,
            total_ops_credit_cents=300,
            total_bonus_cents=120,
            total_consumed_cents=777,
            total_ops_debit_cents=0,
            legacy_unclassified_cents=0,
            total_paid_orders=1,
            last_payment_at=datetime(2026, 3, 25, 11, 0, 0),
        )
        fake_db = _FakeDB(
            execute_results=[
                _FakeAllResult([(user, station_link, station)]),
                _FakeScalarsResult([]),
                _FakeEntityResult(finance_summary),
            ],
            scalar_results=[120, 450],
        )

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        with patch.object(admin_module, "decrypt_api_key", side_effect=lambda value: "sk_cc_test_admin_visible" if value else None):
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.get("/admin/users/u_1")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertIn("finance_summary", payload)
        self.assertEqual(payload["finance_summary"]["total_paid_balance_cents"], 4999)
        self.assertEqual(payload["finance_summary"]["consumed_7d_cents"], 120)
        self.assertEqual(payload["finance_summary"]["consumed_30d_cents"], 450)
        self.assertEqual(payload["finance_summary"]["current_balance_cents"], 4321)
        self.assertEqual(payload["station_attribution"]["station_id"], "st_1")
        self.assertEqual(payload["station_attribution"]["station_name"], "Alpha Station")
        self.assertEqual(payload["station_attribution"]["station_owner_user_id"], "u_owner")

    async def test_user_detail_exposes_key_policy_and_shared_balance(self) -> None:
        user = SimpleNamespace(
            id="u_1",
            username="alice",
            external_id="ext_alice",
            status="active",
            balance=2500,
            token_limit=None,
            token_used=0,
            input_tokens_used=0,
            output_tokens_used=0,
            request_limit_per_minute=None,
            request_limit_per_day=None,
            created_at=datetime(2026, 3, 25, 10, 0, 0),
            updated_at=datetime(2026, 3, 25, 10, 0, 0),
        )
        api_key = SimpleNamespace(
            id="k_api",
            kind="api",
            status="active",
            key_hash="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            encrypted_key='{"v":1,"alg":"fernet-sha256","token":"gAAAAABoBocdya4b4vsRvw5TTAZ1q3fhdEqjzHJO8xU5zJ5wI4_7-Vih82hAz5YJ2vVY4jAO2AK4etkqvP-MU0ExyqusywOwBA=="}',
            created_at=datetime(2026, 3, 25, 11, 0, 0),
            last_used_at=None,
        )
        session_key = SimpleNamespace(
            id="k_session",
            kind="session",
            status="active",
            key_hash="fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210",
            encrypted_key=None,
            created_at=datetime(2026, 3, 25, 12, 0, 0),
            last_used_at=datetime(2026, 3, 25, 12, 30, 0),
        )
        finance_summary = SimpleNamespace(
            user_id="u_1",
            initialized_from_history=1,
            total_paid_rmb_cents=0,
            total_paid_balance_cents=0,
            total_ops_credit_cents=0,
            total_bonus_cents=0,
            total_consumed_cents=0,
            total_ops_debit_cents=0,
            legacy_unclassified_cents=0,
            total_paid_orders=0,
            last_payment_at=None,
        )
        fake_db = _FakeDB(
            execute_results=[
                _FakeAllResult([(user, None, None)]),
                _FakeScalarsResult([session_key, api_key]),
                _FakeEntityResult(finance_summary),
            ],
            scalar_results=[0, 0],
        )

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        with patch.object(admin_module, "decrypt_api_key", side_effect=lambda value: "sk_cc_test_admin_visible" if value else None):
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.get("/admin/users/u_1")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["key_display_policy"]["raw_key_recoverable"], True)
        self.assertEqual(payload["key_display_policy"]["shared_balance_scope"], "user")
        keys_by_id = {item["id"]: item for item in payload["keys"]}
        self.assertEqual(keys_by_id["k_session"]["kind"], "session")
        self.assertEqual(keys_by_id["k_session"]["shared_balance"], 2500)
        self.assertEqual(keys_by_id["k_session"]["fingerprint"], "fedcba987654")
        self.assertIsNone(keys_by_id["k_session"]["raw_key"])
        self.assertEqual(keys_by_id["k_api"]["kind"], "api")
        self.assertEqual(keys_by_id["k_api"]["fingerprint"], "0123456789ab")
        self.assertEqual(keys_by_id["k_api"]["raw_key"], "sk_cc_test_admin_visible")

    async def test_list_keys_exposes_kind_fingerprint_and_shared_balance(self) -> None:
        key = SimpleNamespace(
            id="k_api",
            user_id="u_1",
            kind="api",
            status="active",
            key_hash="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            encrypted_key='{"v":1,"alg":"fernet-sha256","token":"gAAAAABoBocdya4b4vsRvw5TTAZ1q3fhdEqjzHJO8xU5zJ5wI4_7-Vih82hAz5YJ2vVY4jAO2AK4etkqvP-MU0ExyqusywOwBA=="}',
            created_at=datetime(2026, 3, 25, 11, 0, 0),
            last_used_at=datetime(2026, 3, 25, 12, 30, 0),
        )
        user = SimpleNamespace(
            id="u_1",
            username="alice",
            external_id="ext_alice",
            balance=2500,
        )
        fake_db = _FakeDB(execute_results=[_FakeAllResult([(key, user)])])

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        with patch.object(admin_module, "decrypt_api_key", return_value="sk_cc_test_admin_visible"):
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.get("/admin/keys")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload[0]["kind"], "api")
        self.assertEqual(payload[0]["shared_balance"], 2500)
        self.assertEqual(payload[0]["shared_balance_usd"], 25.0)
        self.assertEqual(payload[0]["fingerprint"], "0123456789ab")
        self.assertEqual(payload[0]["raw_key"], "sk_cc_test_admin_visible")


if __name__ == "__main__":
    unittest.main()
