import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
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
from app.payment_common import quote_payment_cents
from app.router import registry as model_registry


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


class _FakeSummaryResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeDB:
    def __init__(self, *, execute_results=None, scalar_results=None):
        self._execute_results = list(execute_results or [])
        self._scalar_results = list(scalar_results or [])
        self.queries = []
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, _query):
        self.queries.append(_query)
        if not self._execute_results:
            raise AssertionError("unexpected execute call")
        return self._execute_results.pop(0)

    async def scalar(self, _query):
        self.queries.append(_query)
        if not self._scalar_results:
            raise AssertionError("unexpected scalar call")
        return self._scalar_results.pop(0)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    def add(self, obj):
        self.added.append(obj)


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
        admin_module._settings.model_alias_overrides_path = ""
        model_registry.clear_runtime_alias_overrides()
        model_registry._initialized = False

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

    def test_admin_ui_initial_load_only_loads_active_page(self) -> None:
        admin_html = (Path(admin_module.__file__).parent / "static" / "admin.html").read_text()

        self.assertIn("function loadCurrentPage()", admin_html)
        self.assertIn("function loadAll() {\n      loadCurrentPage();\n    }", admin_html)
        self.assertNotIn(
            "loadUsers();\n      loadUsage();\n      loadFinanceSummary();\n      loadModelAliases();",
            admin_html,
        )

    def test_admin_ui_wires_analytics_page_loader(self) -> None:
        admin_html = (Path(admin_module.__file__).parent / "static" / "admin.html").read_text()

        self.assertIn('data-page="analytics"', admin_html)
        self.assertIn('id="page-analytics"', admin_html)
        self.assertIn("analytics: loadAnalytics,", admin_html)
        self.assertIn("async function loadAnalytics()", admin_html)

    async def test_request_logs_expose_provider_alias_and_usage_units(self) -> None:
        log = SimpleNamespace(
            created_at=datetime(2026, 3, 25, 12, 34, 56),
            api_key_id="k_img",
            endpoint="images/generations",
            model="gemini-image",
            input_tokens=0,
            output_tokens=0,
            cached_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            image_count=2,
            provider_model="gemini-3.1-flash-image",
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
        self.assertEqual(item["provider_model"], "gemini-3.1-flash-image")
        self.assertEqual(item["customer_model_alias"], "gemini-image")
        self.assertEqual(item["usage_unit_type"], "images")
        self.assertEqual(item["usage_unit_count"], 2)
        self.assertEqual(item["image_count"], 2)
        self.assertEqual(item["cache_read_tokens"], 0)
        self.assertEqual(item["cache_creation_tokens"], 0)
        self.assertEqual(item["billable_sku"], "gemini-image")
        self.assertEqual(item["upstream_request_id"], "req_img_123")

    async def test_admin_can_reset_user_password(self) -> None:
        user = SimpleNamespace(id="u_1")
        account = SimpleNamespace(
            username="alice",
            password_hash="old-hash",
            status="active",
            failed_attempts=4,
            locked_until=datetime(2026, 5, 1, 12, 0, 0),
        )
        fake_db = _FakeDB(
            execute_results=[
                _FakeScalarOneResult(user),
                _FakeScalarOneResult(account),
            ]
        )

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        with patch.object(admin_module, "hash_password", AsyncMock(return_value="new-hash")) as hashed:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/admin/users/u_1/reset-password",
                    json={"new_password": "new-secret"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "password_reset")
        self.assertEqual(response.json()["username"], "alice")
        hashed.assert_awaited_once_with("new-secret")
        self.assertEqual(account.password_hash, "new-hash")
        self.assertEqual(account.failed_attempts, 0)
        self.assertIsNone(account.locked_until)
        self.assertEqual(fake_db.commits, 1)

    async def test_admin_reset_user_password_requires_existing_account(self) -> None:
        user = SimpleNamespace(id="u_1")
        fake_db = _FakeDB(
            execute_results=[
                _FakeScalarOneResult(user),
                _FakeScalarOneResult(None),
            ]
        )

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/admin/users/u_1/reset-password",
                json={"new_password": "new-secret"},
            )

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["detail"], "account not found")
        self.assertEqual(fake_db.commits, 0)

    async def test_admin_reset_user_password_validates_length(self) -> None:
        fake_db = _FakeDB(execute_results=[_FakeEntityResult(None)])

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/admin/users/u_1/reset-password",
                json={"new_password": "short"},
            )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(fake_db.commits, 0)

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
            cache_read_tokens=0,
            cache_creation_tokens=0,
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
                _FakeSummaryResult((1, 10, 5, 0, 0, 0, 0, 15)),
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
        self.assertEqual(payload["summary"]["cost_cents"], 1)
        self.assertEqual(payload["summary"]["total_tokens"], 15)
        self.assertEqual(payload["summary"]["cache_read_tokens"], 0)
        self.assertEqual(payload["summary"]["cache_creation_tokens"], 0)
        self.assertEqual(payload["data"][0]["api_key_id"], "k_selected")
        self.assertNotIn("provider_model", payload["data"][0])

    async def test_user_usage_summary_covers_all_filtered_rows_not_current_page(self) -> None:
        user = SimpleNamespace(id="u_1")
        log = SimpleNamespace(
            created_at=datetime(2026, 5, 1, 18, 23, 19),
            api_key_id="k_selected",
            endpoint="responses",
            model="gpt-5.4",
            input_tokens=10,
            output_tokens=5,
            cached_tokens=2,
            cache_read_tokens=0,
            cache_creation_tokens=0,
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
                _FakeScalarResult(3500),
                _FakeSummaryResult((85, 1_200_000, 116_675, 400_000, 0, 12_345, 3, 1_316_675)),
                _FakeScalarsResult([log]),
            ]
        )

        with patch.object(openai_module, "authenticate_user", AsyncMock(return_value=user)):
            payload = await openai_module.get_usage(
                SimpleNamespace(),
                fake_db,
                limit=15,
                offset=0,
                api_key_id="k_selected",
            )

        self.assertEqual(payload["total"], 3500)
        self.assertEqual(len(payload["data"]), 1)
        self.assertEqual(payload["summary"]["cost_cents"], 85)
        self.assertEqual(payload["summary"]["cost_usd"], 0.85)
        self.assertEqual(payload["summary"]["total_tokens"], 1_316_675)
        self.assertEqual(payload["summary"]["cached_tokens"], 400_000)
        self.assertEqual(payload["summary"]["cache_read_tokens"], 400_000)
        self.assertEqual(payload["summary"]["cache_creation_tokens"], 12_345)
        self.assertEqual(payload["summary"]["image_count"], 3)
        self.assertEqual(payload["data"][0]["cache_read_tokens"], 2)
        self.assertEqual(payload["data"][0]["cache_creation_tokens"], 0)
        self.assertNotIn("provider_model", payload["data"][0])

    async def test_usage_date_filters_use_china_day_boundaries(self) -> None:
        user = SimpleNamespace(id="u_1")
        fake_db = _FakeDB(
            execute_results=[
                _FakeScalarResult(0),
                _FakeSummaryResult((0, 0, 0, 0, 0, 0, 0, 0)),
                _FakeScalarsResult([]),
            ]
        )

        with patch.object(openai_module, "authenticate_user", AsyncMock(return_value=user)):
            await openai_module.get_usage(
                SimpleNamespace(),
                fake_db,
                start_date="2026-05-03",
                end_date="2026-05-03",
            )

        compiled = fake_db.queries[0].compile()
        params = list(compiled.params.values())
        self.assertIn(datetime(2026, 5, 2, 16, 0), params)
        self.assertIn(datetime(2026, 5, 3, 16, 0), params)

    async def test_usage_iso_end_filter_can_be_exclusive(self) -> None:
        user = SimpleNamespace(id="u_1")
        fake_db = _FakeDB(
            execute_results=[
                _FakeScalarResult(0),
                _FakeSummaryResult((0, 0, 0, 0, 0, 0, 0, 0)),
                _FakeScalarsResult([]),
            ]
        )

        with patch.object(openai_module, "authenticate_user", AsyncMock(return_value=user)):
            await openai_module.get_usage(
                SimpleNamespace(),
                fake_db,
                start_date="2026-05-02T16:00:00.000Z",
                end_date="2026-05-03T16:00:00.000Z",
                end_exclusive=True,
            )

        compiled = fake_db.queries[0].compile()
        self.assertIn("created_at < ", str(compiled))
        params = list(compiled.params.values())
        self.assertIn(datetime(2026, 5, 2, 16, 0), params)
        self.assertIn(datetime(2026, 5, 3, 16, 0), params)

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

    async def test_admin_analytics_overview_uses_existing_daily_aggregates(self) -> None:
        usage_row = SimpleNamespace(
            active_users=3,
            requests_total=45,
            input_tokens=1000,
            output_tokens=250,
            tokens_total=1250,
            images_total=6,
            cost_cents=321,
        )
        fake_db = _FakeDB(
            execute_results=[_FakeSummaryResult(usage_row)],
            scalar_results=[12, 10, 999],
        )

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/admin/analytics/overview?period=today")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["period"], "today")
        self.assertEqual(payload["days"], 1)
        self.assertEqual(payload["total_users"], 12)
        self.assertEqual(payload["active_users"], 10)
        self.assertEqual(payload["active_users_period"], 3)
        self.assertEqual(payload["requests_total"], 45)
        self.assertEqual(payload["tokens_total"], 1250)
        self.assertEqual(payload["images_total"], 6)
        self.assertEqual(payload["user_charge_cents"], 321)
        self.assertEqual(payload["paid_cents"], 999)
        self.assertEqual(payload["net_cashflow_cents"], 678)

    async def test_admin_analytics_top_users_returns_ranked_usage_rows(self) -> None:
        row = SimpleNamespace(
            user_id="u_1",
            username="alice",
            email="alice@example.com",
            external_id="ext_alice",
            balance=4321,
            requests_total=9,
            input_tokens=100,
            output_tokens=50,
            tokens_total=150,
            images_total=2,
            cost_cents=88,
        )
        fake_db = _FakeDB(execute_results=[_FakeAllResult([row])])

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/admin/analytics/top-users?period=7d&metric=cost_cents&limit=5")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["period"], "7d")
        self.assertEqual(payload["metric"], "cost_cents")
        self.assertEqual(payload["data"][0]["user_id"], "u_1")
        self.assertEqual(payload["data"][0]["display_name"], "alice")
        self.assertEqual(payload["data"][0]["cost_cents"], 88)
        self.assertEqual(payload["data"][0]["balance_cents"], 4321)

    async def test_admin_analytics_low_balance_users_estimates_days_remaining(self) -> None:
        row = SimpleNamespace(
            user_id="u_low",
            username="low",
            email=None,
            external_id=None,
            balance=120,
            requests_total=12,
            tokens_total=300,
            images_total=0,
            cost_cents=420,
        )
        fake_db = _FakeDB(execute_results=[_FakeAllResult([row])])

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/admin/analytics/low-balance-users?period=7d&limit=5")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        item = payload["data"][0]
        self.assertEqual(item["user_id"], "u_low")
        self.assertEqual(item["avg_daily_cost_cents"], 60)
        self.assertEqual(item["estimated_days_remaining"], 2.0)
        self.assertEqual(item["risk_level"], "critical")

    async def test_admin_analytics_errors_returns_recent_error_rollup(self) -> None:
        recent_log = SimpleNamespace(
            created_at=datetime(2026, 5, 12, 8, 30, 0),
            user_id="u_1",
            endpoint="responses",
            model="gpt-5.4",
            status_code=429,
            duration_ms=1800,
            route_reason="catalog:gpt-5.4:legacy_explicit",
            upstream_request_id="req_123",
        )
        user = SimpleNamespace(username="alice", email=None, external_id=None, id="u_1")
        fake_db = _FakeDB(
            execute_results=[
                _FakeAllResult([(429, 3)]),
                _FakeAllResult([("gpt-5.4", 3)]),
                _FakeAllResult([(recent_log, user)]),
            ],
            scalar_results=[10, 3],
        )

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/admin/analytics/errors?period=today&limit=5")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["total_requests"], 10)
        self.assertEqual(payload["failed_requests"], 3)
        self.assertEqual(payload["error_rate"], 0.3)
        self.assertEqual(payload["by_status"][0]["status_code"], 429)
        self.assertEqual(payload["by_model"][0]["model"], "gpt-5.4")
        self.assertEqual(payload["recent"][0]["user"], "alice")

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
        fake_db = _FakeDB(execute_results=[_FakeEntityResult(None)])

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
                    json={
                        "name": "基础月卡 套餐",
                        "money": "129.00",
                        "pay_type": "alipay",
                        "product_id": "monthly_basic",
                    },
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
        self.assertEqual(payload["expected_cents"], 38000)
        self.assertEqual(fake_db.commits, 1)

    def test_product_quote_uses_selected_product_id(self) -> None:
        self.assertEqual(quote_payment_cents("29.90", "monthly_light"), 7500)
        self.assertEqual(quote_payment_cents("299.00", "monthly_flagship"), 100000)
        self.assertEqual(quote_payment_cents("299.00", "addon_project"), 110000)
        self.assertEqual(quote_payment_cents("499.00", "addon_ultra"), 200000)

    def test_product_quote_rejects_unknown_or_mismatched_product(self) -> None:
        with self.assertRaises(payment_module.PaymentConfirmError):
            quote_payment_cents("129.00", "missing_product")
        with self.assertRaises(payment_module.PaymentConfirmError):
            quote_payment_cents("29.90", "monthly_basic")

    async def test_admin_payment_orders_expose_product_metadata(self) -> None:
        orders = [
            SimpleNamespace(
                id="po_1",
                user_id="u_1",
                order_no="CC_monthly_basic",
                amount_rmb="129.00",
                add_balance_cents=38000,
                product_id="monthly_basic",
                status="confirmed",
                trade_no="trade_1",
                pay_url="https://code.nxslq.top/submit.php?...",
                created_at=datetime(2026, 3, 25, 11, 0, 0),
                confirmed_at=datetime(2026, 3, 25, 11, 2, 0),
            ),
            SimpleNamespace(
                id="po_2",
                user_id="u_1",
                order_no="CC_addon_ultra",
                amount_rmb="499.00",
                add_balance_cents=200000,
                product_id="addon_ultra",
                status="pending",
                trade_no=None,
                pay_url="https://code.nxslq.top/submit.php?...",
                created_at=datetime(2026, 3, 26, 11, 0, 0),
                confirmed_at=None,
            ),
        ]
        fake_db = _FakeDB(execute_results=[_FakeScalarsResult(orders)])

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/admin/payment-orders")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload[0]["product_id"], "monthly_basic")
        self.assertEqual(payload[0]["product_name"], "基础月卡")
        self.assertEqual(payload[0]["product_kind"], "monthly")
        self.assertEqual(payload[0]["product_balance_cents"], 38000)
        self.assertEqual(payload[1]["product_id"], "addon_ultra")
        self.assertEqual(payload[1]["product_name"], "超大包")
        self.assertEqual(payload[1]["product_kind"], "addon")
        self.assertEqual(payload[1]["product_min_plan_rank"], 3)

    async def test_list_orders_returns_current_user_payment_history(self) -> None:
        user = SimpleNamespace(id="u_1")
        orders = [
            SimpleNamespace(
                id="po_1",
                order_no="CC_confirmed",
                amount_rmb="9.90",
                add_balance_cents=4999,
                status="confirmed",
                trade_no="trade_1",
                created_at=datetime(2026, 3, 25, 11, 0, 0),
                confirmed_at=datetime(2026, 3, 25, 11, 2, 0),
            ),
            SimpleNamespace(
                id="po_2",
                order_no="CC_pending",
                amount_rmb="29.90",
                add_balance_cents=14999,
                status="pending",
                trade_no=None,
                created_at=datetime(2026, 3, 26, 11, 0, 0),
                confirmed_at=None,
            ),
        ]
        fake_db = _FakeDB(execute_results=[_FakeScalarsResult(orders)])

        async def fake_get_db():
            yield fake_db

        async def fake_authenticate_user(_request, _db):
            return user

        original_authenticate_user = payment_module.authenticate_user
        payment_module.authenticate_user = fake_authenticate_user
        app.dependency_overrides[payment_module.get_db] = fake_get_db

        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.get(
                    "/v1/orders",
                    headers={"Authorization": "Bearer sk_cc_test"},
                )
        finally:
            payment_module.authenticate_user = original_authenticate_user

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual([row["order_no"] for row in payload], ["CC_confirmed", "CC_pending"])
        self.assertEqual(payload[0]["status"], "confirmed")
        self.assertEqual(payload[0]["add_balance_usd"], 49.99)
        self.assertEqual(payload[1]["status"], "pending")

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
                _FakeEntityResult(None),
                _FakeScalarsResult([]),
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
        self.assertEqual(payload["billing_summary"]["available_cents"], 4321)
        self.assertEqual(payload["billing_summary"]["legacy_balance_cents"], 4321)
        self.assertEqual(payload["billing"]["legacy_balance"]["remaining_cents"], 4321)
        self.assertEqual(payload["station_attribution"]["station_id"], "st_1")
        self.assertEqual(payload["station_attribution"]["station_name"], "Alpha Station")
        self.assertEqual(payload["station_attribution"]["station_owner_user_id"], "u_owner")

    async def test_update_user_can_clear_usage_limits(self) -> None:
        user = SimpleNamespace(
            id="u_1",
            status="active",
            balance=294,
            token_limit=1_000_000,
            token_used=1_000_000,
            input_tokens_used=1_000_000,
            output_tokens_used=9_400,
            request_limit_per_minute=60,
            request_limit_per_day=1000,
        )
        fake_db = _FakeDB(execute_results=[_FakeEntityResult(user)])

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.patch(
                "/admin/users/u_1",
                json={
                    "token_limit": None,
                    "request_limit_per_minute": None,
                    "request_limit_per_day": None,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(user.token_limit)
        self.assertIsNone(user.request_limit_per_minute)
        self.assertIsNone(user.request_limit_per_day)
        payload = response.json()
        self.assertIsNone(payload["token_limit"])
        self.assertIsNone(payload["request_limit_per_minute"])
        self.assertIsNone(payload["request_limit_per_day"])
        self.assertEqual(fake_db.commits, 1)

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
                _FakeEntityResult(None),
                _FakeScalarsResult([]),
                _FakeScalarsResult([]),
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
        self.assertEqual(payload["billing_summary"]["available_cents"], 2500)
        self.assertEqual(payload["billing_summary"]["legacy_balance_cents"], 2500)
        self.assertEqual(payload["billing"]["available"]["remaining_cents"], 2500)

    async def test_admin_can_adjust_subscription(self) -> None:
        user = SimpleNamespace(id="u_1", balance=1200, status="active")
        sub = SimpleNamespace(
            id="sub_1",
            user_id="u_1",
            plan_id="monthly_light",
            status="active",
            period_start=datetime(2026, 5, 1, 0, 0, 0),
            period_end=datetime(2026, 5, 31, 0, 0, 0),
            paid_until=datetime(2026, 5, 31, 0, 0, 0),
            quota_cents=7500,
            used_cents=500,
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
                _FakeEntityResult(user),
                _FakeEntityResult(sub),
                _FakeEntityResult(sub),
                _FakeScalarsResult([]),
                _FakeScalarsResult([]),
                _FakeEntityResult(finance_summary),
            ],
            scalar_results=[0, 0],
        )

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.patch(
                "/admin/users/u_1/subscription",
                json={
                    "plan_id": "monthly_basic",
                    "status": "active",
                    "period_start": "2026-05-01T00:00:00Z",
                    "period_end": "2026-05-31T00:00:00Z",
                    "paid_until": "2026-06-15T00:00:00Z",
                    "quota_cents": 40000,
                    "used_cents": 3000,
                    "note": "manual adjust",
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(sub.plan_id, "monthly_basic")
        self.assertEqual(sub.quota_cents, 40000)
        self.assertEqual(sub.used_cents, 3000)
        self.assertEqual(payload["billing_summary"]["subscription_plan_id"], "monthly_basic")
        self.assertEqual(fake_db.commits, 1)
        self.assertTrue(any(getattr(item, "entry_type", "") == "admin_subscription_adjust" for item in fake_db.added))

    async def test_admin_can_grant_traffic_pack(self) -> None:
        user = SimpleNamespace(id="u_1", balance=800, status="active")
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
                _FakeEntityResult(user),
                _FakeEntityResult(None),
                _FakeScalarsResult([]),
                _FakeScalarsResult([]),
                _FakeEntityResult(finance_summary),
            ],
            scalar_results=[0, 0],
        )

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/admin/users/u_1/traffic-packs",
                json={
                    "product_id": "addon_project",
                    "remaining_cents": 90000,
                    "expires_at": "2026-12-01T00:00:00Z",
                    "note": "campaign bonus",
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["traffic_pack_id"][:3], "tp_")
        granted_pack = next(item for item in fake_db.added if getattr(item, "id", "").startswith("tp_"))
        self.assertEqual(granted_pack.product_id, "addon_project")
        self.assertEqual(granted_pack.remaining_cents, 90000)
        self.assertEqual(fake_db.commits, 1)
        self.assertTrue(any(getattr(item, "entry_type", "") == "admin_traffic_pack_grant" for item in fake_db.added))

    async def test_admin_can_update_traffic_pack(self) -> None:
        user = SimpleNamespace(id="u_1", balance=600, status="active")
        pack = SimpleNamespace(
            id="tp_1",
            user_id="u_1",
            product_id="addon_boost",
            status="active",
            original_cents=28000,
            remaining_cents=12000,
            expires_at=datetime(2026, 9, 1, 0, 0, 0),
            created_at=datetime(2026, 5, 2, 0, 0, 0),
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
                _FakeEntityResult(pack),
                _FakeEntityResult(user),
                _FakeEntityResult(None),
                _FakeScalarsResult([pack]),
                _FakeScalarsResult([]),
                _FakeEntityResult(finance_summary),
            ],
            scalar_results=[0, 0],
        )

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.patch(
                "/admin/traffic-packs/tp_1",
                json={
                    "status": "disabled",
                    "remaining_cents": 5000,
                    "expires_at": "2026-10-01T00:00:00Z",
                    "note": "manual pack edit",
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(pack.status, "disabled")
        self.assertEqual(pack.remaining_cents, 5000)
        self.assertEqual(payload["traffic_pack_id"], "tp_1")
        self.assertEqual(fake_db.commits, 1)
        self.assertTrue(any(getattr(item, "entry_type", "") == "admin_traffic_pack_adjust" for item in fake_db.added))

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

    async def test_admin_model_alias_update_persists_db_override_and_refreshes_registry(self) -> None:
        catalog = {
            "default_text_model": "alias-a",
            "models": [
                {
                    "id": "alias-a",
                    "owned_by": "coincoin",
                    "provider_name": "OpenAI",
                    "provider_model": "gpt-5.4",
                    "capabilities": ["chat/completions", "responses"],
                    "routing_mode": "direct",
                    "delivery_lane": "upstream_direct",
                    "upstream_model": "gpt-5.4",
                    "upstream_url": "https://legacy.example/v1",
                    "api_key": "legacy-key",
                    "auth_style": "bearer",
                    "billable_sku": "alias-a-text",
                },
                {
                    "id": "alias-b",
                    "owned_by": "coincoin",
                    "provider_name": "OpenAI",
                    "provider_model": "gpt-5.5",
                    "capabilities": ["chat/completions", "responses"],
                    "routing_mode": "direct",
                    "delivery_lane": "upstream_direct",
                    "upstream_model": "gpt-5.5",
                    "upstream_url": "https://legacy.example/v1",
                    "api_key": "legacy-key",
                    "auth_style": "bearer",
                    "billable_sku": "alias-b-text",
                },
            ],
        }
        originals = {
            "model_catalog_json": admin_module._settings.model_catalog_json,
            "model_alias_overrides_path": admin_module._settings.model_alias_overrides_path,
        }
        fake_db = _FakeDB(execute_results=[_FakeScalarsResult([]), _FakeEntityResult(None)])

        async def fake_get_db():
            yield fake_db

        with tempfile.NamedTemporaryFile("w+", encoding="utf-8") as override_file:
            try:
                admin_module._settings.model_catalog_json = json.dumps(catalog)
                admin_module._settings.model_alias_overrides_path = ""
                model_registry._initialized = False
                model_registry.init_from_settings()
                app.dependency_overrides[admin_module.get_db] = fake_get_db
                app.dependency_overrides[admin_module.admin_guard] = lambda: None

                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    response = await client.patch(
                        "/admin/model-aliases/alias-a",
                        json={"target_alias": "alias-b"},
                    )

                self.assertEqual(response.status_code, 200, response.text)
                payload = response.json()
                self.assertEqual(payload["alias"]["id"], "alias-a")
                self.assertTrue(payload["alias"]["override_active"])
                self.assertEqual(payload["alias"]["upstream_model"], "gpt-5.5")

                self.assertEqual(len(fake_db.added), 1)
                self.assertEqual(fake_db.added[0].alias_id, "alias-a")
                self.assertEqual(fake_db.added[0].upstream_model, "gpt-5.5")
                self.assertEqual(fake_db.commits, 1)
                self.assertEqual(Path(override_file.name).read_text(encoding="utf-8"), "")

                resolved = model_registry.resolve_public_model("alias-a", "responses")
                self.assertEqual(resolved.backend.model_id, "gpt-5.5")
            finally:
                admin_module._settings.model_catalog_json = originals["model_catalog_json"]
                admin_module._settings.model_alias_overrides_path = originals["model_alias_overrides_path"]
                app.dependency_overrides.pop(admin_module.get_db, None)
                model_registry._initialized = False

    async def test_admin_model_alias_update_rejects_arbitrary_upstream_model(self) -> None:
        catalog = {
            "default_text_model": "alias-a",
            "models": [
                {
                    "id": "alias-a",
                    "owned_by": "coincoin",
                    "provider_name": "OpenAI",
                    "provider_model": "gpt-5.4",
                    "capabilities": ["chat/completions", "responses"],
                    "routing_mode": "direct",
                    "delivery_lane": "upstream_direct",
                    "upstream_model": "gpt-5.4",
                    "upstream_url": "https://legacy.example/v1",
                    "api_key": "legacy-key",
                    "auth_style": "bearer",
                    "billable_sku": "alias-a-text",
                },
                {
                    "id": "image-a",
                    "owned_by": "coincoin",
                    "provider_name": "OpenAI",
                    "provider_model": "gpt-image-1",
                    "capabilities": ["images/generations", "images/edits"],
                    "routing_mode": "direct",
                    "delivery_lane": "upstream_direct",
                    "upstream_model": "gpt-image-1",
                    "upstream_url": "https://legacy.example/v1",
                    "api_key": "legacy-key",
                    "auth_style": "bearer",
                    "billable_sku": "image-a",
                },
            ],
        }
        originals = {
            "model_catalog_json": admin_module._settings.model_catalog_json,
            "model_alias_overrides_path": admin_module._settings.model_alias_overrides_path,
        }
        fake_db = _FakeDB(execute_results=[_FakeScalarsResult([])])

        async def fake_get_db():
            yield fake_db

        with tempfile.NamedTemporaryFile("w+", encoding="utf-8") as override_file:
            try:
                admin_module._settings.model_catalog_json = json.dumps(catalog)
                admin_module._settings.model_alias_overrides_path = override_file.name
                model_registry._initialized = False
                model_registry.init_from_settings()
                app.dependency_overrides[admin_module.get_db] = fake_get_db
                app.dependency_overrides[admin_module.admin_guard] = lambda: None

                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    response = await client.patch(
                        "/admin/model-aliases/alias-a",
                        json={"provider_model": "gpt-image-1", "upstream_model": "gpt-image-1"},
                    )

                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("not compatible", response.json()["detail"])
                self.assertEqual(Path(override_file.name).read_text(encoding="utf-8"), "")
            finally:
                admin_module._settings.model_catalog_json = originals["model_catalog_json"]
                admin_module._settings.model_alias_overrides_path = originals["model_alias_overrides_path"]
                app.dependency_overrides.pop(admin_module.get_db, None)
                model_registry.clear_runtime_alias_overrides()
                model_registry._initialized = False

    async def test_admin_can_switch_claude_compat_provider(self) -> None:
        originals = {
            "claude_compat_provider": admin_module._settings.claude_compat_provider,
            "claude_compat_base_url": admin_module._settings.claude_compat_base_url,
        }
        setting_row = None
        fake_db = _FakeDB(execute_results=[_FakeEntityResult(setting_row)])

        async def fake_get_db():
            yield fake_db

        try:
            admin_module._settings.claude_compat_provider = "upstream_direct"
            admin_module._settings.claude_compat_base_url = "https://kiro-go.example"
            model_registry.clear_runtime_system_settings()
            model_registry._initialized = False
            model_registry.init_from_settings()
            app.dependency_overrides[admin_module.get_db] = fake_get_db
            app.dependency_overrides[admin_module.admin_guard] = lambda: None

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.patch(
                    "/admin/settings/claude-compat",
                    json={"provider": "kiro_go"},
                )

            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertEqual(payload["provider"], "kiro_go")
            self.assertEqual(len(fake_db.added), 1)
            self.assertEqual(fake_db.added[0].setting_key, "claude_compat_provider")
            self.assertEqual(fake_db.added[0].setting_value, "kiro_go")
            self.assertEqual(fake_db.commits, 1)
            self.assertEqual(model_registry.current_claude_compat_provider(), "kiro_go")
        finally:
            admin_module._settings.claude_compat_provider = originals["claude_compat_provider"]
            admin_module._settings.claude_compat_base_url = originals["claude_compat_base_url"]
            model_registry.clear_runtime_system_settings()
            model_registry._initialized = False
            app.dependency_overrides.pop(admin_module.get_db, None)

    async def test_admin_rejects_kiro_go_switch_without_base_url(self) -> None:
        originals = {
            "claude_compat_provider": admin_module._settings.claude_compat_provider,
            "claude_compat_base_url": admin_module._settings.claude_compat_base_url,
        }
        fake_db = _FakeDB()

        async def fake_get_db():
            yield fake_db

        try:
            admin_module._settings.claude_compat_provider = "upstream_direct"
            admin_module._settings.claude_compat_base_url = ""
            model_registry.clear_runtime_system_settings()
            model_registry._initialized = False
            model_registry.init_from_settings()
            app.dependency_overrides[admin_module.get_db] = fake_get_db
            app.dependency_overrides[admin_module.admin_guard] = lambda: None

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.patch(
                    "/admin/settings/claude-compat",
                    json={"provider": "kiro_go"},
                )

            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("CLAUDE_COMPAT_BASE_URL", response.json()["detail"])
            self.assertEqual(fake_db.commits, 0)
        finally:
            admin_module._settings.claude_compat_provider = originals["claude_compat_provider"]
            admin_module._settings.claude_compat_base_url = originals["claude_compat_base_url"]
            model_registry.clear_runtime_system_settings()
            model_registry._initialized = False
            app.dependency_overrides.pop(admin_module.get_db, None)


if __name__ == "__main__":
    unittest.main()
