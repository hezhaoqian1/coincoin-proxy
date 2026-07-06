import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.main import app
import app.main as main_module
import app.admin as admin_module
import app.epay as epay_module
import app.payment as payment_module
import app.proxy as proxy_module
import app.webhook as webhook_module
import app.openai_compat as openai_module
from app.payment_common import quote_payment_cents
from app.router import registry as model_registry
from app.schemas import AdminProviderChannelCreate, AdminProviderChannelUpdate


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

    async def flush(self):
        pass

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
        model_registry.clear_runtime_pricing_overrides()
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

    async def test_admin_with_token_serves_admin_ui_for_acceptance(self) -> None:
        original_token = admin_module._settings.admin_token
        admin_module._settings.admin_token = "admin-secret"
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.get("/admin?token=admin-secret")

            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("模型转发例外", response.text)
            self.assertIn("缓存计费例外", response.text)
            self.assertIn("model-routing-overrides", response.text)
            self.assertIn("model-pricing-overrides", response.text)
        finally:
            admin_module._settings.admin_token = original_token

    async def test_admin_without_token_keeps_spa_fallback(self) -> None:
        original_token = admin_module._settings.admin_token
        admin_module._settings.admin_token = "admin-secret"
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.get("/admin")

            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("模型转发例外", response.text)
            self.assertNotIn("model-routing-overrides", response.text)
        finally:
            admin_module._settings.admin_token = original_token

    async def test_admin_with_empty_configured_token_keeps_spa_fallback(self) -> None:
        original_token = admin_module._settings.admin_token
        admin_module._settings.admin_token = ""
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.get("/admin?token=")

            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("模型转发例外", response.text)
            self.assertNotIn("model-routing-overrides", response.text)
        finally:
            admin_module._settings.admin_token = original_token

    def test_admin_ui_initial_load_only_loads_active_page(self) -> None:
        admin_html = (Path(admin_module.__file__).parent / "static" / "admin.html").read_text()

        self.assertIn("function loadCurrentPage()", admin_html)
        self.assertIn("function initCollapsibleCards(scope = document)", admin_html)
        self.assertIn("function loadAll() {\n      initCollapsibleCards();\n      loadCurrentPage();\n    }", admin_html)
        self.assertNotIn(
            "loadUsers();\n      loadUsage();\n      loadFinanceSummary();\n      loadModelAliases();",
            admin_html,
        )

    def test_admin_ui_wires_provider_channel_card_collapses(self) -> None:
        admin_html = (Path(admin_module.__file__).parent / "static" / "admin.html").read_text()

        self.assertIn("collapsible-card", admin_html)
        self.assertIn("card-collapse-btn", admin_html)
        self.assertIn("toggleCollapsibleCard('provider-channel-monitor')", admin_html)
        self.assertIn("toggleCollapsibleCard('provider-channels')", admin_html)
        self.assertIn("toggleCollapsibleCard('model-channel-routes')", admin_html)
        self.assertNotIn("toggleCollapsibleCard('provider-channel-stability')", admin_html)
        self.assertNotIn("toggleCollapsibleCard('default-provider-channels')", admin_html)
        self.assertIn("cc_admin_card_collapsed:", admin_html)
        self.assertIn('value="anthropic_compatible"', admin_html)
        self.assertIn('value="x-api-key"', admin_html)
        self.assertIn("anthropic_messages", admin_html)
        self.assertIn("handleProviderChannelTypeChange", admin_html)
        self.assertIn("handleModelChannelRouteChannelChange", admin_html)
        self.assertIn("modelChannelRouteChannelPriority", admin_html)
        self.assertIn("route override", admin_html)
        self.assertIn("inherits channel", admin_html)
        self.assertIn("留空时继承渠道本身的优先级和权重", admin_html)

    def test_provider_channel_schema_accepts_anthropic_x_api_key(self) -> None:
        created = AdminProviderChannelCreate(
            name="Claude relay",
            provider_platform="new_api",
            channel_type="anthropic_compatible",
            base_url="https://claude-relay.example",
            api_key="sk-test",
            auth_style="x-api-key",
            capabilities=["chat/completions"],
        )
        updated = AdminProviderChannelUpdate(
            channel_type="anthropic_compatible",
            auth_style="anthropic_x_api_key",
            capabilities=["chat/completions"],
        )

        self.assertEqual(created.channel_type, "anthropic_compatible")
        self.assertEqual(created.auth_style, "x-api-key")
        self.assertEqual(updated.auth_style, "anthropic_x_api_key")

    def test_admin_ui_wires_analytics_page_loader(self) -> None:
        admin_html = (Path(admin_module.__file__).parent / "static" / "admin.html").read_text()

        self.assertIn('data-page="analytics"', admin_html)
        self.assertIn('id="page-analytics"', admin_html)
        self.assertIn("analytics: loadAnalytics,", admin_html)
        self.assertIn("async function loadAnalytics()", admin_html)

    def test_admin_ui_wires_rolling_usage_leaderboards(self) -> None:
        admin_html = (Path(admin_module.__file__).parent / "static" / "admin.html").read_text()

        self.assertIn("滚动用量排行榜", admin_html)
        self.assertIn("usageLeaderboard1hBody", admin_html)
        self.assertIn("usageLeaderboard4hBody", admin_html)
        self.assertIn("usageLeaderboard24hBody", admin_html)
        self.assertIn("/admin/usage/leaderboard?window=", admin_html)
        self.assertIn("function loadUsageLeaderboards()", admin_html)
        self.assertIn("loadUsageLeaderboards();", admin_html)

    def test_admin_ui_wires_user_usage_sort(self) -> None:
        admin_html = (Path(admin_module.__file__).parent / "static" / "admin.html").read_text()

        self.assertIn('id="userUsageSort"', admin_html)
        self.assertIn('value="1d">近 1 天消耗', admin_html)
        self.assertIn('value="7d">近 7 天消耗', admin_html)
        self.assertIn("params.set('usage_sort', usageSort)", admin_html)
        self.assertIn("周期消耗", admin_html)
        self.assertIn("u.period_usage", admin_html)

    def test_admin_quick_create_uses_admin_user_endpoint_with_password(self) -> None:
        admin_html = (Path(admin_module.__file__).parent / "static" / "admin.html").read_text()

        self.assertIn('id="newPassword"', admin_html)
        self.assertIn("payload.password = password", admin_html)
        self.assertIn("fetch('/admin/users'", admin_html)
        self.assertIn("...adminHeaders()", admin_html)
        self.assertNotIn("fetch('/v1/keys/activate'", admin_html)

    def test_admin_ui_wires_user_model_override_sections(self) -> None:
        admin_html = (Path(admin_module.__file__).parent / "static" / "admin.html").read_text()

        self.assertIn("模型转发例外", admin_html)
        self.assertIn("缓存计费例外", admin_html)
        self.assertIn("saveUserRoutingOverride", admin_html)
        self.assertIn("saveNewUserRoutingOverride", admin_html)
        self.assertIn("userRoutingNewModel", admin_html)
        self.assertIn("userRoutingNewTarget", admin_html)
        self.assertIn("deleteUserRoutingOverride", admin_html)
        self.assertIn("saveUserPricingOverride", admin_html)
        self.assertIn("saveNewUserPricingOverride", admin_html)
        self.assertIn("userPricingNewModel", admin_html)
        self.assertIn("deleteUserPricingOverride", admin_html)

    def test_admin_ui_surfaces_provider_fallback_observability(self) -> None:
        admin_html = (Path(admin_module.__file__).parent / "static" / "admin.html").read_text()

        self.assertIn("24h Fallback", admin_html)
        self.assertIn("Route / Channel", admin_html)
        self.assertIn("fallback_from_channel_id", admin_html)
        self.assertIn("route_attempt", admin_html)
        self.assertNotIn("渠道稳定性", admin_html)
        self.assertNotIn("系统默认渠道", admin_html)
        self.assertNotIn("/admin/provider-channels/stability", admin_html)
        self.assertIn("主动监控", admin_html)
        self.assertIn("/admin/provider-channel-monitors", admin_html)
        self.assertIn("data-monitor-period", admin_html)
        self.assertIn("providerChannelMonitorSearch", admin_html)
        self.assertIn("providerChannelMonitorStatusFilter", admin_html)
        self.assertIn("providerChannelMonitorRunModal", admin_html)
        self.assertIn("providerChannelMonitorHistoryModal", admin_html)
        self.assertIn("setProviderChannelMonitorStatus", admin_html)
        self.assertIn("openProviderChannelMonitorHistory", admin_html)
        self.assertIn("createMonitorFromDiscoveredModel", admin_html)

    async def test_provider_channels_includes_system_default_channels(self) -> None:
        catalog = {
            "default_text_model": "codex-legacy",
            "models": [
                {
                    "id": "codex-legacy",
                    "owned_by": "openai",
                    "provider_name": "OpenAI",
                    "capabilities": ["chat/completions", "responses"],
                    "routing_mode": "legacy_auto",
                    "billable_sku": "legacy-codex",
                    "metadata": {
                        "execution_pool": "cpa_coding_pool",
                        "legacy_default_slot": "premium",
                    },
                },
                {
                    "id": "gemini-balanced",
                    "owned_by": "google",
                    "provider_name": "Google",
                    "provider_model": "gemini-2.5-flash-lite",
                    "capabilities": ["chat/completions", "responses"],
                    "routing_mode": "direct",
                    "delivery_lane": "cpa_gemini",
                    "upstream_model": "gemini-2.5-flash-lite",
                    "upstream_url": "https://gemini.example/v1",
                    "api_key": "gemini-key",
                    "auth_style": "bearer",
                    "billable_sku": "gemini-balanced-text",
                    "metadata": {
                        "channel_id": "gemini-cpa-primary",
                        "priority": 0,
                        "weight": 1,
                        "allowed_fails": "${COINCOIN_GEMINI_CPA_ALLOWED_FAILS:-4}",
                        "cooldown_seconds": "${COINCOIN_GEMINI_CPA_COOLDOWN_SECONDS:-12.5}",
                    },
                },
            ],
        }
        originals = {
            "model_catalog_json": admin_module._settings.model_catalog_json,
            "model_alias_overrides_path": admin_module._settings.model_alias_overrides_path,
        }
        fake_db = _FakeDB(
            execute_results=[
                _FakeScalarsResult([]),
                _FakeAllResult([]),
                _FakeAllResult([]),
                _FakeScalarsResult([]),
            ]
        )

        async def fake_get_db():
            yield fake_db

        try:
            admin_module._settings.model_catalog_json = json.dumps(catalog)
            admin_module._settings.model_alias_overrides_path = ""
            model_registry._initialized = False
            app.dependency_overrides[admin_module.get_db] = fake_get_db
            app.dependency_overrides[admin_module.admin_guard] = lambda: None

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.get("/admin/provider-channels")

            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertEqual(payload["channels"], [])
            defaults = {item["id"]: item for item in payload["default_channels"]}
            self.assertIn("system:legacy:cpa_coding_pool:premium", defaults)
            self.assertIn("system:cpa_gemini:gemini-cpa-primary", defaults)
            self.assertEqual(defaults["system:legacy:cpa_coding_pool:premium"]["model_count"], 1)
            self.assertEqual(defaults["system:cpa_gemini:gemini-cpa-primary"]["allowed_fails"], 4)
            self.assertEqual(defaults["system:cpa_gemini:gemini-cpa-primary"]["cooldown_seconds"], 12.5)
            self.assertIn("gemini-balanced", defaults["system:cpa_gemini:gemini-cpa-primary"]["public_models"])
        finally:
            admin_module._settings.model_catalog_json = originals["model_catalog_json"]
            admin_module._settings.model_alias_overrides_path = originals["model_alias_overrides_path"]
            app.dependency_overrides.pop(admin_module.get_db, None)
            model_registry._initialized = False

    async def test_provider_channels_include_billing_stats(self) -> None:
        channel = SimpleNamespace(
            id="ch_northstar",
            name="North Star",
            provider_platform="sub2api",
            channel_type="openai_compatible",
            base_url="https://sub2api.example/v1",
            encrypted_api_key="cipher",
            auth_style="bearer",
            status="active",
            priority=0,
            weight=1,
            allowed_fails=3,
            cooldown_seconds=30,
            capabilities="responses,chat/completions",
            provider_account_fingerprint="acct_northstar",
            cost_tier="premium",
            notes="",
            updated_by="admin",
            created_at=datetime(2026, 6, 1, 11, 0, 0),
            updated_at=datetime(2026, 6, 1, 11, 0, 0),
        )
        route_row = SimpleNamespace(channel_id="ch_northstar", route_count=2)
        billing_row = SimpleNamespace(
            channel_id="ch_northstar",
            last_1h_cents=123,
            last_4h_cents=456,
            today_cents=789,
            total_cents=3210,
        )
        runtime_row = SimpleNamespace(
            channel_id="ch_northstar",
            fail_count=0,
            cooldown_until=None,
            last_success_at=datetime(2026, 6, 1, 12, 0, 0),
            last_failure_at=None,
            last_error_code="",
            rolling_latency_ms=900,
        )
        fake_db = _FakeDB(
            execute_results=[
                _FakeScalarsResult([channel]),
                _FakeAllResult([route_row]),
                _FakeAllResult([billing_row]),
                _FakeScalarsResult([runtime_row]),
            ]
        )

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        with patch.object(admin_module, "_system_default_channel_payloads", return_value=[]), patch.object(
            admin_module, "_provider_channel_key_fingerprint", return_value="fp_test"
        ):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.get("/admin/provider-channels")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(len(payload["channels"]), 1)
        item = payload["channels"][0]
        self.assertEqual(item["id"], "ch_northstar")
        self.assertEqual(item["route_count"], 2)
        self.assertEqual(
            item["billing_stats"],
            {
                "last_1h_cents": 123,
                "last_4h_cents": 456,
                "today_cents": 789,
                "total_cents": 3210,
            },
        )
        self.assertTrue(item["api_key_configured"])
        self.assertEqual(item["api_key_fingerprint"], "fp_test")

    async def test_provider_channel_stability_aggregates_request_logs(self) -> None:
        channel = SimpleNamespace(
            id="ch_primary",
            name="North Star",
            provider_platform="sub2api",
            channel_type="openai_compatible",
            base_url="https://sub2api.example/v1",
            encrypted_api_key=None,
            auth_style="bearer",
            status="active",
            priority=0,
            weight=1,
            allowed_fails=3,
            cooldown_seconds=30,
            capabilities="responses,chat/completions",
            provider_account_fingerprint="acct_test",
            cost_tier="premium",
            notes="",
            updated_by="admin",
            created_at=datetime(2026, 6, 1, 11, 0, 0),
            updated_at=datetime(2026, 6, 1, 11, 0, 0),
        )
        stats_row = SimpleNamespace(
            channel_id="ch_primary",
            provider_platform="sub2api",
            channel_type="openai_compatible",
            provider_account_fingerprint="acct_test",
            requests=10,
            success_requests=9,
            failed_requests=1,
            fallback_in_requests=2,
            avg_latency_ms=1234,
            max_latency_ms=9000,
            last_seen_at=datetime(2026, 6, 1, 12, 0, 0),
        )
        fallback_out_row = SimpleNamespace(channel_id="ch_primary", fallback_out_requests=3)
        recent_log = SimpleNamespace(
            channel_id="ch_primary",
            status_code=200,
            duration_ms=800,
            created_at=datetime(2026, 6, 1, 12, 0, 0),
            route_attempt=1,
            route_reason="channel_fallback:500",
            model="gpt-5.3-codex",
            customer_model_alias="gpt-5.3-codex",
        )
        fake_db = _FakeDB(
            execute_results=[
                _FakeScalarsResult([channel]),
                _FakeScalarsResult([]),
                _FakeAllResult([stats_row]),
                _FakeAllResult([fallback_out_row]),
                _FakeScalarsResult([recent_log]),
            ]
        )

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/admin/provider-channels/stability?period=7d")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["summary"]["requests"], 10)
        self.assertEqual(payload["summary"]["fallback_out_requests"], 3)
        item = payload["items"][0]
        self.assertEqual(item["channel_id"], "ch_primary")
        self.assertEqual(item["name"], "North Star")
        self.assertEqual(item["availability_rate"], 0.9)
        self.assertEqual(item["fallback_in_requests"], 2)
        self.assertEqual(item["fallback_out_requests"], 3)
        self.assertEqual(item["health_status"], "degraded")
        self.assertEqual(item["recent"][0]["status"], "fallback")

    async def test_provider_channel_monitors_expose_active_probe_rollups(self) -> None:
        monitor = SimpleNamespace(
            id="cmon_primary",
            channel_id="ch_primary",
            name="North Star gpt-5.3",
            endpoint="responses",
            primary_model="gpt-5.3-codex",
            extra_models='["gpt-5.4"]',
            status="active",
            interval_seconds=60,
            timeout_seconds=30,
            last_checked_at=datetime(2026, 6, 1, 12, 0, 0),
            last_status="operational",
            last_latency_ms=1200,
            last_ping_latency_ms=18,
            last_message="ok",
            created_at=datetime(2026, 6, 1, 11, 0, 0),
            updated_at=datetime(2026, 6, 1, 11, 0, 0),
        )
        channel = SimpleNamespace(
            id="ch_primary",
            name="North Star",
            provider_platform="sub2api",
            channel_type="openai_compatible",
            base_url="https://sub2api.example/v1",
        )
        other_monitor = SimpleNamespace(
            id="cmon_other",
            channel_id="ch_other",
            name="Other disabled",
            endpoint="responses",
            primary_model="gpt-5.5",
            extra_models="[]",
            status="disabled",
            interval_seconds=60,
            timeout_seconds=30,
            last_checked_at=None,
            last_status="",
            last_latency_ms=0,
            last_ping_latency_ms=0,
            last_message="",
            created_at=datetime(2026, 6, 1, 11, 0, 0),
            updated_at=datetime(2026, 6, 1, 11, 0, 0),
        )
        other_channel = SimpleNamespace(
            id="ch_other",
            name="Other",
            provider_platform="newapi",
            channel_type="openai_compatible",
            base_url="https://other.example/v1",
        )
        availability_row = SimpleNamespace(
            monitor_id="cmon_primary",
            model="gpt-5.3-codex",
            total_checks=10,
            operational_count=8,
            degraded_count=1,
            failed_count=1,
            error_count=0,
            sum_latency_ms=12000,
            count_latency=10,
            sum_ping_latency_ms=180,
            count_ping_latency=10,
        )
        history = SimpleNamespace(
            monitor_id="cmon_primary",
            model="gpt-5.3-codex",
            status="operational",
            latency_ms=1200,
            ping_latency_ms=18,
            status_code=200,
            message="ok",
            checked_at=datetime(2026, 6, 1, 12, 0, 0),
        )
        fake_db = _FakeDB(
            execute_results=[
                _FakeAllResult([(monitor, channel), (other_monitor, other_channel)]),
                _FakeAllResult([availability_row]),
                _FakeScalarsResult([history]),
            ]
        )

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get(
                "/admin/provider-channel-monitors?period=15d&search=north&status_filter=active&provider=sub2api&channel_id=ch_primary"
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["period"], "15d")
        self.assertEqual(payload["summary"]["total"], 1)
        self.assertEqual(payload["summary"]["active"], 1)
        self.assertEqual(payload["summary"]["operational"], 1)
        self.assertEqual(len(payload["items"]), 1)
        item = payload["items"][0]
        self.assertEqual(item["id"], "cmon_primary")
        self.assertEqual(item["channel_name"], "North Star")
        self.assertEqual(item["models"], ["gpt-5.3-codex", "gpt-5.4"])
        self.assertEqual(item["availability_rate"], 0.9)
        self.assertEqual(item["avg_latency_ms"], 1200)
        self.assertEqual(item["avg_ping_latency_ms"], 18)
        self.assertEqual(item["timeline"][0]["status"], "operational")

    async def test_provider_channel_upstream_models_uses_v1_fallback_and_masks_key(self) -> None:
        channel = SimpleNamespace(
            id="ch_sub2api",
            name="Sub2API",
            base_url="https://sub2api.example",
            encrypted_api_key=admin_module.encrypt_api_key("sk-test-secret"),
            auth_style="bearer",
        )

        class _GetDB:
            async def get(self, model, key):
                if model is admin_module.ProviderChannel and key == channel.id:
                    return channel
                return None

        class _Response:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

        calls = []

        class _Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def get(self, url, headers):
                calls.append((url, dict(headers)))
                if url == "https://sub2api.example/models":
                    return _Response(404, {"error": "not found"})
                return _Response(
                    200,
                    {
                        "data": [
                            {"id": "gpt-5.3-codex", "object": "model", "owned_by": "sub2api"},
                            {"id": "claude-sonnet-4.6", "object": "model", "owned_by": "sub2api"},
                        ]
                    },
                )

        with patch.object(admin_module.httpx, "AsyncClient", _Client):
            payload = await admin_module.list_provider_channel_upstream_models(channel.id, db=_GetDB())

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["models_url"], "https://sub2api.example/v1/models")
        self.assertEqual(payload["recommended_base_url"], "https://sub2api.example/v1")
        self.assertEqual([item["id"] for item in payload["models"]], ["gpt-5.3-codex", "claude-sonnet-4.6"])
        self.assertEqual(calls[0][1]["authorization"], "Bearer sk-test-secret")
        self.assertEqual(calls[1][1]["authorization"], "Bearer sk-test-secret")
        self.assertNotIn("sk-test-secret", json.dumps(payload))

    async def test_provider_channel_connection_uses_api_key_auth_style(self) -> None:
        channel = SimpleNamespace(
            id="ch_azure",
            name="Azure-style",
            base_url="https://azure.example/openai/v1",
            encrypted_api_key=admin_module.encrypt_api_key("sk-azure-secret"),
            auth_style="azure",
        )

        class _GetDB:
            async def get(self, model, key):
                if model is admin_module.ProviderChannel and key == channel.id:
                    return channel
                return None

        class _Response:
            status_code = 200

            def json(self):
                return {"data": [{"id": "gpt-5.4", "object": "model"}]}

        calls = []

        class _Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def get(self, url, headers):
                calls.append((url, dict(headers)))
                return _Response()

        with patch.object(admin_module.httpx, "AsyncClient", _Client):
            payload = await admin_module.test_provider_channel_connection(channel.id, db=_GetDB())

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["model_count"], 1)
        self.assertEqual(payload["sample_models"][0]["id"], "gpt-5.4")
        self.assertEqual(calls[0][0], "https://azure.example/openai/v1/models")
        self.assertEqual(calls[0][1]["api-key"], "sk-azure-secret")
        self.assertNotIn("sk-azure-secret", json.dumps(payload))

    async def test_anthropic_provider_channel_falls_back_to_messages_probe(self) -> None:
        channel = SimpleNamespace(
            id="ch_anthropic",
            name="Claude relay",
            base_url="https://claude-relay.example",
            encrypted_api_key=admin_module.encrypt_api_key("sk-anthropic-secret"),
            auth_style="x-api-key",
            channel_type="anthropic_compatible",
        )

        class _GetDB:
            async def get(self, model, key):
                if model is admin_module.ProviderChannel and key == channel.id:
                    return channel
                return None

        class _Response:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

        calls = []

        class _Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def get(self, url, headers):
                calls.append(("GET", url, dict(headers), None))
                return _Response(404, {"error": {"message": "models not supported"}})

            async def post(self, url, headers, json):
                calls.append(("POST", url, dict(headers), dict(json)))
                return _Response(
                    200,
                    {
                        "id": "msg_probe",
                        "type": "message",
                        "role": "assistant",
                        "model": json.get("model"),
                        "content": [{"type": "text", "text": "pong"}],
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 2, "output_tokens": 1},
                    },
                )

        with patch.object(admin_module.httpx, "AsyncClient", _Client):
            payload = await admin_module.list_provider_channel_upstream_models(channel.id, db=_GetDB())

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["discovery_mode"], "messages_probe")
        self.assertEqual(payload["models_url"], "https://claude-relay.example/v1/messages")
        self.assertEqual(payload["models"][0]["id"], "claude-fable-5")
        self.assertEqual(payload["models"][0]["suggested_public_model_id"], "claude-fable-5")
        self.assertEqual(calls[0][0], "GET")
        self.assertEqual(calls[0][1], "https://claude-relay.example/v1/models")
        self.assertEqual(calls[0][2]["x-api-key"], "sk-anthropic-secret")
        self.assertEqual(calls[0][2]["anthropic-version"], "2023-06-01")
        self.assertEqual(calls[1][0], "POST")
        self.assertEqual(calls[1][1], "https://claude-relay.example/v1/messages")
        self.assertEqual(calls[1][2]["x-api-key"], "sk-anthropic-secret")
        self.assertEqual(calls[1][3]["model"], "claude-fable-5")
        self.assertNotIn("sk-anthropic-secret", json.dumps(payload))

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
            channel_id="ch_backup",
            channel_type="openai_compatible",
            provider_platform="sub2api",
            provider_account_fingerprint="acct_test",
            fallback_from_channel_id="ch_primary",
            route_attempt=1,
            price_version=7,
            pricing_mode="multiplier",
            model_multiplier=1.5,
            output_multiplier=2.0,
            cache_read_multiplier=1.0,
            image_multiplier=1.0,
            video_multiplier=1.0,
            base_price_input_per_million=250,
            base_price_output_per_million=1500,
            effective_cached_input_per_million=375.0,
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
        self.assertEqual(item["price_version"], 7)
        self.assertEqual(item["pricing_mode"], "multiplier")
        self.assertEqual(item["cache_read_multiplier"], 1.0)
        self.assertEqual(item["effective_cached_input_per_million"], 375.0)
        self.assertEqual(item["channel_id"], "ch_backup")
        self.assertEqual(item["provider_platform"], "sub2api")
        self.assertEqual(item["fallback_from_channel_id"], "ch_primary")
        self.assertEqual(item["route_attempt"], 1)

    async def test_invalidate_user_key_cache_is_best_effort_when_query_fails(self) -> None:
        class _FailingDB:
            async def execute(self, _query):
                raise RuntimeError("db unavailable after commit")

        await admin_module._invalidate_user_key_cache(_FailingDB(), "u_1")

    async def test_ops_health_reports_provider_fallback_activity(self) -> None:
        recent_log = SimpleNamespace(
            created_at=datetime(2026, 6, 1, 12, 0, 0),
            status_code=500,
            endpoint="responses",
            model="gpt-5.3-codex",
            duration_ms=13000,
            route_reason="channel_fallback:500",
            channel_id="ch_backup",
            channel_type="openai_compatible",
            provider_platform="sub2api",
            fallback_from_channel_id="ch_primary",
            route_attempt=1,
            upstream_request_id="req_fallback",
        )
        user = SimpleNamespace(username="alice", email=None, external_id=None, id="u_1")
        fake_db = _FakeDB(
            execute_results=[
                _FakeScalarResult(10),
                _FakeScalarResult(2),
                _FakeScalarResult(3),
                _FakeScalarOneResult(datetime(2026, 6, 1, 12, 5, 0)),
                _FakeAllResult([(500, 2)]),
                _FakeAllResult([("gpt-5.3-codex", 2)]),
                _FakeAllResult([(recent_log, user)]),
            ]
        )

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/admin/ops/health")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["traffic"]["fallback_requests"], 3)
        self.assertEqual(payload["traffic"]["fallback_rate"], 0.3)
        self.assertEqual(payload["errors"]["recent"][0]["channel_id"], "ch_backup")
        self.assertEqual(payload["errors"]["recent"][0]["fallback_from_channel_id"], "ch_primary")
        self.assertEqual(payload["errors"]["recent"][0]["route_attempt"], 1)

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

    async def test_admin_can_create_user_with_password_and_key(self) -> None:
        fake_db = _FakeDB(
            execute_results=[
                _FakeScalarOneResult(None),
                _FakeScalarOneResult(None),
                _FakeScalarOneResult(None),
                _FakeScalarOneResult(None),
                _FakeScalarOneResult(None),
            ]
        )

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        with patch.object(admin_module, "generate_id", side_effect=["u_new", "acc_new", "k_new"]), patch.object(
            admin_module, "generate_api_key", return_value="sk_cc_new"
        ), patch.object(admin_module, "hash_key", return_value="hashed-key"), patch.object(
            admin_module, "encrypt_api_key", return_value="encrypted-key"
        ), patch.object(admin_module, "generate_referral_code", return_value="REF2026"), patch.object(
            admin_module, "hash_password", AsyncMock(return_value="hashed-password")
        ) as hashed, patch.object(
            admin_module, "ensure_finance_summary_initialized", AsyncMock()
        ), patch.object(
            admin_module, "increment_finance_summary", AsyncMock()
        ):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/admin/users",
                    json={"username": "alice", "external_id": "ext_alice", "password": "new-secret"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["user_id"], "u_new")
        self.assertEqual(payload["username"], "alice")
        self.assertEqual(payload["external_id"], "ext_alice")
        self.assertEqual(payload["api_key"], "sk_cc_new")
        self.assertEqual(payload["key_id"], "k_new")
        self.assertEqual(payload["account_status"], "active")
        hashed.assert_awaited_once_with("new-secret")

        added_by_type = {type(item).__name__: item for item in fake_db.added}
        self.assertEqual(added_by_type["User"].username, "alice")
        self.assertEqual(added_by_type["User"].external_id, "ext_alice")
        self.assertEqual(added_by_type["Account"].username, "alice")
        self.assertEqual(added_by_type["Account"].linked_user_id, "u_new")
        self.assertEqual(added_by_type["Account"].password_hash, "hashed-password")
        self.assertEqual(added_by_type["ApiKey"].user_id, "u_new")
        self.assertEqual(added_by_type["ApiKey"].encrypted_key, "encrypted-key")
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
                _FakeSummaryResult((1, 10, 5, 0, 0, 0, 0, 0, 15)),
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
                _FakeSummaryResult((85, 1_200_000, 116_675, 400_000, 0, 12_345, 3, 0, 1_316_675)),
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
                _FakeSummaryResult((0, 0, 0, 0, 0, 0, 0, 0, 0)),
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
                _FakeSummaryResult((0, 0, 0, 0, 0, 0, 0, 0, 0)),
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
        fake_db = _FakeDB(scalar_results=[12, 10, 987654, 45, 6, 0, 999, 321])

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
        self.assertEqual(payload["total_videos_today"], 0)
        self.assertEqual(payload["paid_today_cents"], 999)
        self.assertEqual(payload["consumed_today_cents"], 321)

    async def test_admin_analytics_overview_today_uses_rolling_24h_request_logs(self) -> None:
        request_row = SimpleNamespace(
            active_users=3,
            requests_total=45,
            input_tokens=1000,
            output_tokens=250,
            tokens_total=1250,
            images_total=6,
            cost_cents=321,
        )
        admin_module._analytics_balance_cache.clear()
        fake_db = _FakeDB(
            execute_results=[_FakeSummaryResult(request_row)],
            scalar_results=[12, 10, 999],
        )

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        with patch.object(admin_module, "_positive_balance_users_count", AsyncMock(return_value=4)):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.get("/admin/analytics/overview?period=today")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["period"], "today")
        self.assertEqual(payload["period_label"], "近24小时")
        self.assertEqual(payload["days"], 1)
        self.assertEqual(payload["window_hours"], 24)
        self.assertEqual(payload["total_users"], 12)
        self.assertEqual(payload["active_users"], 10)
        self.assertEqual(payload["positive_balance_users"], 4)
        self.assertEqual(payload["users_with_balance"], 4)
        self.assertEqual(payload["active_users_period"], 3)
        self.assertEqual(payload["requests_total"], 45)
        self.assertEqual(payload["tokens_total"], 1250)
        self.assertEqual(payload["images_total"], 6)
        self.assertEqual(payload["user_charge_cents"], 321)
        self.assertEqual(payload["paid_cents"], 999)
        self.assertEqual(payload["net_cashflow_cents"], 678)
        overview_query = str(fake_db.queries[-1].compile())
        self.assertIn("coincoin_request_logs", overview_query)
        self.assertIn("created_at >=", overview_query)
        self.assertNotIn("coincoin_usage_daily", overview_query)

    async def test_admin_analytics_top_users_today_uses_rolling_24h_request_logs(self) -> None:
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
            response = await client.get("/admin/analytics/top-users?period=today&metric=cost_cents&limit=5")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["period"], "today")
        self.assertEqual(payload["period_label"], "近24小时")
        self.assertEqual(payload["window_hours"], 24)
        self.assertEqual(payload["data"][0]["user_id"], "u_1")
        self.assertEqual(payload["data"][0]["cost_cents"], 88)
        query_text = str(fake_db.queries[-1].compile())
        self.assertIn("coincoin_request_logs", query_text)
        self.assertIn("created_at >=", query_text)
        self.assertNotIn("coincoin_usage_daily", query_text)

    async def test_admin_usage_leaderboard_supports_rolling_windows(self) -> None:
        row = SimpleNamespace(
            user_id="u_hot",
            username="hot-user",
            email=None,
            external_id=None,
            balance=1200,
            requests_total=12,
            input_tokens=800,
            output_tokens=200,
            tokens_total=1000,
            images_total=3,
            cost_cents=456,
        )
        fake_db = _FakeDB(execute_results=[_FakeAllResult([row])])

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/admin/usage/leaderboard?window=4h&metric=cost_cents&limit=5")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["window"], "4h")
        self.assertEqual(payload["window_hours"], 4)
        self.assertEqual(payload["window_label"], "近 4 小时")
        self.assertEqual(payload["data"][0]["rank"], 1)
        self.assertEqual(payload["data"][0]["display_name"], "hot-user")
        self.assertEqual(payload["data"][0]["cost_cents"], 456)
        self.assertEqual(payload["data"][0]["tokens_total"], 1000)
        query_text = str(fake_db.queries[-1].compile())
        self.assertIn("coincoin_request_logs", query_text)
        self.assertIn("created_at >=", query_text)
        self.assertNotIn("coincoin_usage_daily", query_text)

    async def test_admin_users_can_sort_by_recent_usage(self) -> None:
        user = SimpleNamespace(
            id="u_hot",
            username="hot-user",
            email="hot@example.com",
            email_verified_at=None,
            external_id=None,
            status="active",
            balance=1200,
            token_limit=None,
            token_used=1000,
            input_tokens_used=800,
            output_tokens_used=200,
            request_limit_per_minute=None,
            request_limit_per_day=None,
            referral_code=None,
            referred_by=None,
            created_at=datetime(2026, 6, 1, 10, 0, 0),
            updated_at=datetime(2026, 6, 1, 10, 0, 0),
        )
        fake_db = _FakeDB(execute_results=[_FakeAllResult([(user, None, None, 12, 1000, 3, 0, 456)])])

        with patch.object(admin_module, "_admin_billing_state", AsyncMock(return_value={})):
            result = await admin_module.list_users(usage_sort="7d", db=fake_db)

        self.assertEqual(result[0]["id"], "u_hot")
        self.assertEqual(result[0]["period_usage"]["period"], "7d")
        self.assertEqual(result[0]["period_usage"]["window_hours"], 168)
        self.assertEqual(result[0]["period_usage"]["requests_total"], 12)
        self.assertEqual(result[0]["period_usage"]["tokens_total"], 1000)
        self.assertEqual(result[0]["period_usage"]["images_total"], 3)
        self.assertEqual(result[0]["period_usage"]["videos_total"], 0)
        self.assertEqual(result[0]["period_usage"]["cost_cents"], 456)
        query_text = str(fake_db.queries[-1].compile())
        self.assertIn("coincoin_request_logs", query_text)
        self.assertIn("created_at >=", query_text)
        self.assertIn("period_cost_cents", query_text)

    async def test_admin_revenue_margin_today_keeps_previous_day_rows_inside_24h_window(self) -> None:
        fake_db = _FakeDB(
            execute_results=[
                _FakeAllResult([
                    SimpleNamespace(day=date(2026, 5, 14), paid_cents=400, paid_users=1),
                    SimpleNamespace(day=date(2026, 5, 15), paid_cents=600, paid_users=1),
                ]),
                _FakeAllResult([
                    SimpleNamespace(day=date(2026, 5, 14), user_charge_cents=300, upstream_cost_cents=120, requests_total=3),
                    SimpleNamespace(day=date(2026, 5, 15), user_charge_cents=700, upstream_cost_cents=230, requests_total=7),
                ]),
            ],
            scalar_results=[50, 80],
        )

        payload = await admin_module.analytics_revenue_margin(period="today", db=fake_db)

        self.assertEqual(payload["period"], "today")
        self.assertEqual(payload["period_label"], "近24小时")
        self.assertEqual(payload["window_hours"], 24)
        self.assertEqual(payload["paid_cents"], 1000)
        self.assertEqual(payload["user_charge_cents"], 1000)
        self.assertEqual(payload["upstream_cost_cents"], 350)
        self.assertEqual(payload["gross_margin_cents"], 650)
        self.assertEqual(payload["package_consumption_cents"], 50)
        self.assertEqual(payload["failed_payment_cents"], 80)
        self.assertTrue({"2026-05-14", "2026-05-15"}.issubset({item["day"] for item in payload["daily"]}))
        self.assertEqual(sum(item["requests_total"] for item in payload["daily"]), 10)
        query_text = "\n".join(str(query.compile()) for query in fake_db.queries)
        self.assertIn("coincoin_payment_orders.confirmed_at >=", query_text)
        self.assertIn("coincoin_request_logs.created_at >=", query_text)

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

    async def test_admin_model_latency_diagnostics_breaks_down_slow_model(self) -> None:
        summary = SimpleNamespace(
            min_latency_ms=1200,
            avg_latency_ms=53712,
            max_latency_ms=90000,
            tokens=1054898,
            user_charge_cents=197,
            fallback_requests=1,
        )
        user_row = SimpleNamespace(
            user_id="u_slow",
            username="slow-user",
            email=None,
            external_id=None,
            requests=2,
            avg_latency_ms=60000,
            max_latency_ms=90000,
            user_charge_cents=120,
        )
        route_row = SimpleNamespace(
            route_reason="catalog:gpt-5.3-codex:legacy_explicit",
            requests=2,
            avg_latency_ms=60000,
            max_latency_ms=90000,
            failed_requests=0,
            fallback_requests=1,
        )
        endpoint_row = SimpleNamespace(
            endpoint="responses",
            requests=2,
            avg_latency_ms=60000,
            max_latency_ms=90000,
        )
        hourly_row = SimpleNamespace(
            hour="2026-05-24 00:00:00",
            requests=2,
            avg_latency_ms=60000,
            max_latency_ms=90000,
        )
        slow_log = SimpleNamespace(
            created_at=datetime(2026, 5, 24, 0, 10, 0),
            user_id="u_slow",
            endpoint="responses",
            model="gpt-5.3-codex",
            provider_model="gpt-5.3-codex",
            customer_model_alias="gpt-5.3-codex",
            billable_sku="legacy-gpt-5.3-codex-text",
            duration_ms=90000,
            status_code=200,
            route_reason="catalog:gpt-5.3-codex:legacy_explicit",
            channel_id="ch_backup",
            channel_type="openai_compatible",
            provider_platform="sub2api",
            fallback_from_channel_id="ch_primary",
            route_attempt=1,
            input_tokens=1000,
            output_tokens=200,
            cost_cents=120,
            upstream_request_id="req_slow",
        )
        user = SimpleNamespace(username="slow-user", email=None, external_id=None, id="u_slow")
        fake_db = _FakeDB(
            execute_results=[
                _FakeSummaryResult(summary),
                _FakeAllResult([(1200,), (60000,), (90000,)]),
                _FakeAllResult([user_row]),
                _FakeAllResult([route_row]),
                _FakeAllResult([endpoint_row]),
                _FakeAllResult([(slow_log, user)]),
                _FakeAllResult([(slow_log, user)]),
                _FakeAllResult([hourly_row]),
            ],
            scalar_results=[3, 0, 3, 2, 1],
        )

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get(
                "/admin/model-latency-diagnostics?period=today&model=gpt-5.3-codex&limit=5"
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["model"], "gpt-5.3-codex")
        self.assertEqual(payload["period_label"], "近24小时")
        self.assertEqual(payload["summary"]["requests"], 3)
        self.assertEqual(payload["summary"]["avg_latency_ms"], 53712)
        self.assertEqual(payload["summary"]["fallback_requests"], 1)
        self.assertEqual(payload["summary"]["fallback_rate"], 1 / 3)
        self.assertEqual(payload["summary"]["p95_latency_ms"], 90000)
        self.assertEqual(payload["summary"]["slow_counts"]["ge_60s"], 1)
        self.assertEqual(payload["by_user"][0]["display_name"], "slow-user")
        self.assertEqual(payload["by_route"][0]["route_reason"], "catalog:gpt-5.3-codex:legacy_explicit")
        self.assertEqual(payload["by_route"][0]["fallback_requests"], 1)
        self.assertEqual(payload["slow_requests"][0]["upstream_request_id"], "req_slow")
        self.assertEqual(payload["slow_requests"][0]["fallback_from_channel_id"], "ch_primary")

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
                        "money": "199.00",
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
        self.assertEqual(payload["expected_cents"], 40000)
        self.assertEqual(fake_db.commits, 1)

    def test_product_quote_uses_selected_product_id(self) -> None:
        self.assertEqual(quote_payment_cents("49.90", "monthly_light"), 8000)
        self.assertEqual(quote_payment_cents("399.00", "monthly_flagship"), 100000)
        self.assertEqual(quote_payment_cents("399.00", "addon_project"), 100000)
        self.assertEqual(quote_payment_cents("699.00", "addon_ultra"), 200000)

    def test_product_quote_rejects_unknown_or_mismatched_product(self) -> None:
        with self.assertRaises(payment_module.PaymentConfirmError):
            quote_payment_cents("199.00", "missing_product")
        with self.assertRaises(payment_module.PaymentConfirmError):
            quote_payment_cents("49.90", "monthly_basic")

    async def test_admin_payment_orders_expose_product_metadata(self) -> None:
        orders = [
            SimpleNamespace(
                id="po_1",
                user_id="u_1",
                order_no="CC_monthly_basic",
                amount_rmb="199.00",
                add_balance_cents=40000,
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
                amount_rmb="699.00",
                add_balance_cents=200000,
                product_id="addon_ultra",
                status="pending",
                trade_no=None,
                pay_url="https://code.nxslq.top/submit.php?...",
                created_at=datetime(2026, 3, 26, 11, 0, 0),
                confirmed_at=None,
            ),
        ]
        user = SimpleNamespace(id="u_1", username="alice", email="alice@example.com", external_id="ext_alice")
        fake_db = _FakeDB(execute_results=[_FakeAllResult([(orders[0], user), (orders[1], user)])])

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
        self.assertEqual(payload[0]["product_balance_cents"], 40000)
        self.assertEqual(payload[0]["username"], "alice")
        self.assertEqual(payload[0]["email"], "alice@example.com")
        self.assertEqual(payload[0]["external_id"], "ext_alice")
        self.assertEqual(payload[0]["display_name"], "alice")
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

    async def test_confirm_order_reports_available_balance_for_monthly_product(self) -> None:
        payment_module.settings.epay_api_url = "https://code.nxslq.top/"
        payment_module.settings.epay_pid = "177938431"
        payment_module.settings.epay_key = "j9J4loEx5Qy"

        user = SimpleNamespace(id="u_1", balance=500, referred_by=None, status="active")
        order = SimpleNamespace(
            id="po_1",
            order_no="CC_test_order",
            user_id="u_1",
            amount_rmb="199.00",
            status="pending",
            add_balance_cents=40000,
            product_id="monthly_basic",
            station_id=None,
            trade_no=None,
            confirmed_at=None,
        )
        fake_db = _FakeDB(
            execute_results=[
                _FakeEntityResult(order),
                _FakeEntityResult(order),
                _FakeEntityResult(user),
                _FakeEntityResult(None),
                _FakeEntityResult(None),
                _FakeScalarOneResult(None),
                _FakeAllResult([]),
                _FakeAllResult([]),
                _FakeAllResult([]),
                _FakeAllResult([]),
                _FakeEntityResult(None),
                _FakeScalarsResult([]),
                _FakeEntityResult(None),
                _FakeEntityResult(None),
                _FakeAllResult([]),
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
                        "&type=alipay&name=%E5%9F%BA%E7%A1%80%E6%9C%88%E5%8D%A1&money=199.00&trade_status=TRADE_SUCCESS"
                        "&sign=72d239f97278a7cabea8dad3a276b412&sign_type=MD5",
                    },
                    headers={"Authorization": "Bearer sk_cc_test"},
                )
        finally:
            payment_module.authenticate_user = original_authenticate_user
            payment_module.rate_limiter.allow = original_allow

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["billing_action"], "subscription_start")
        self.assertEqual(payload["new_balance"], 500)
        self.assertEqual(payload["available_cents"], 40500)
        self.assertEqual(payload["available_usd"], 405.0)
        self.assertEqual(payload["added_cents"], 40000)
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
        self.assertEqual(payload["model_routing_overrides"], [])
        self.assertEqual(payload["model_pricing_overrides"], [])

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

    async def test_user_detail_exposes_user_model_override_summaries(self) -> None:
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
        routing_override = SimpleNamespace(
            user_id="u_1",
            public_model_id="claude-opus-4-7",
            provider_model="gpt-5.5",
            upstream_model="gpt-5.5",
            enabled=1,
            updated_by="admin",
            updated_at=datetime(2026, 6, 7, 12, 0, 0),
        )
        pricing_override = SimpleNamespace(
            user_id="u_1",
            public_model_id="claude-opus-4-7",
            cache_read_multiplier_override=1.0,
            updated_by="admin",
            updated_at=datetime(2026, 6, 7, 12, 0, 0),
        )
        fake_db = _FakeDB(
            execute_results=[
                _FakeAllResult([(user, None, None)]),
                _FakeScalarsResult([]),
                _FakeEntityResult(None),
                _FakeScalarsResult([]),
                _FakeScalarsResult([]),
                _FakeScalarsResult([routing_override]),
                _FakeScalarsResult([pricing_override]),
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
            response = await client.get("/admin/users/u_1")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["model_routing_overrides"][0]["public_model_id"], "claude-opus-4-7")
        self.assertEqual(payload["model_routing_overrides"][0]["provider_model"], "gpt-5.5")
        self.assertEqual(payload["model_routing_overrides"][0]["upstream_model"], "gpt-5.5")
        self.assertEqual(payload["model_routing_overrides"][0]["enabled"], True)
        self.assertEqual(payload["model_pricing_overrides"][0]["public_model_id"], "claude-opus-4-7")
        self.assertEqual(payload["model_pricing_overrides"][0]["cache_read_multiplier_override"], 1.0)

    async def test_admin_can_upsert_and_delete_user_model_routing_override(self) -> None:
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
        }
        user = SimpleNamespace(id="u_1")

        class _RoutingOverrideDB:
            def __init__(self) -> None:
                self.routing_row = None
                self.added = []
                self.deleted = []
                self.commits = 0
                self.execute_count = 0

            async def execute(self, _query):
                self.execute_count += 1
                if self.execute_count == 1:
                    return _FakeEntityResult(user)
                if self.execute_count == 2:
                    return _FakeEntityResult(self.routing_row)
                if self.execute_count == 3:
                    return _FakeEntityResult(self.routing_row)
                raise AssertionError("unexpected execute call")

            def add(self, obj):
                self.added.append(obj)
                self.routing_row = obj

            async def delete(self, obj):
                self.deleted.append(obj)
                if obj is self.routing_row:
                    self.routing_row = None

            async def commit(self):
                self.commits += 1

        fake_db = _RoutingOverrideDB()

        async def fake_get_db():
            yield fake_db

        try:
            admin_module._settings.model_catalog_json = json.dumps(catalog)
            model_registry._initialized = False
            model_registry.init_from_settings()
            app.dependency_overrides[admin_module.get_db] = fake_get_db
            app.dependency_overrides[admin_module.admin_guard] = lambda: None

            transport = httpx.ASGITransport(app=app)
            with patch.object(admin_module, "_invalidate_user_key_cache", AsyncMock()) as invalidate_cache:
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    put_response = await client.put(
                        "/admin/users/u_1/model-routing-overrides/alias-a",
                        json={"target_alias": "alias-b", "enabled": True},
                    )
                    delete_response = await client.delete(
                        "/admin/users/u_1/model-routing-overrides/alias-a",
                    )

            self.assertEqual(put_response.status_code, 200, put_response.text)
            payload = put_response.json()
            self.assertEqual(payload["public_model_id"], "alias-a")
            self.assertEqual(payload["provider_model"], "gpt-5.5")
            self.assertEqual(payload["upstream_model"], "gpt-5.5")
            self.assertTrue(payload["enabled"])
            self.assertIsInstance(payload["targets"], list)
            self.assertEqual(fake_db.commits, 2)
            self.assertEqual(len(fake_db.added), 1)
            self.assertEqual(fake_db.added[0].updated_by, "admin")
            self.assertEqual(len(fake_db.deleted), 1)
            self.assertEqual(delete_response.status_code, 200, delete_response.text)
            self.assertEqual(delete_response.json(), {"user_id": "u_1", "public_model_id": "alias-a", "deleted": True})
            self.assertEqual(invalidate_cache.await_count, 2)
        finally:
            admin_module._settings.model_catalog_json = originals["model_catalog_json"]
            model_registry._initialized = False

    async def test_admin_can_upsert_and_delete_user_model_pricing_override(self) -> None:
        catalog = {
            "default_text_model": "priced-a",
            "models": [
                {
                    "id": "priced-a",
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
                    "billable_sku": "priced-a-text",
                    "price_input_per_million": 250,
                    "price_output_per_million": 1500,
                }
            ],
        }
        originals = {
            "model_catalog_json": admin_module._settings.model_catalog_json,
        }
        user = SimpleNamespace(id="u_1")

        class _PricingOverrideDB:
            def __init__(self) -> None:
                self.pricing_row = None
                self.added = []
                self.deleted = []
                self.commits = 0
                self.execute_count = 0

            async def execute(self, _query):
                self.execute_count += 1
                if self.execute_count == 1:
                    return _FakeEntityResult(user)
                if self.execute_count == 2:
                    return _FakeEntityResult(self.pricing_row)
                if self.execute_count == 3:
                    return _FakeEntityResult(self.pricing_row)
                raise AssertionError("unexpected execute call")

            def add(self, obj):
                self.added.append(obj)
                self.pricing_row = obj

            async def delete(self, obj):
                self.deleted.append(obj)
                if obj is self.pricing_row:
                    self.pricing_row = None

            async def commit(self):
                self.commits += 1

        fake_db = _PricingOverrideDB()

        async def fake_get_db():
            yield fake_db

        try:
            admin_module._settings.model_catalog_json = json.dumps(catalog)
            model_registry._initialized = False
            model_registry.init_from_settings()
            app.dependency_overrides[admin_module.get_db] = fake_get_db
            app.dependency_overrides[admin_module.admin_guard] = lambda: None

            transport = httpx.ASGITransport(app=app)
            with patch.object(admin_module, "_invalidate_user_key_cache", AsyncMock()) as invalidate_cache:
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    put_response = await client.put(
                        "/admin/users/u_1/model-pricing-overrides/priced-a",
                        json={"cache_read_multiplier_override": 1.0},
                    )
                    delete_response = await client.delete(
                        "/admin/users/u_1/model-pricing-overrides/priced-a",
                    )

            self.assertEqual(put_response.status_code, 200, put_response.text)
            payload = put_response.json()
            self.assertEqual(payload["public_model_id"], "priced-a")
            self.assertEqual(payload["cache_read_multiplier_override"], 1.0)
            self.assertEqual(fake_db.commits, 2)
            self.assertEqual(len(fake_db.added), 1)
            self.assertEqual(fake_db.added[0].updated_by, "admin")
            self.assertEqual(len(fake_db.deleted), 1)
            self.assertEqual(delete_response.status_code, 200, delete_response.text)
            self.assertEqual(delete_response.json(), {"user_id": "u_1", "public_model_id": "priced-a", "deleted": True})
            self.assertEqual(invalidate_cache.await_count, 2)
        finally:
            admin_module._settings.model_catalog_json = originals["model_catalog_json"]
            model_registry._initialized = False

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
            quota_cents=8000,
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
            original_cents=30000,
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

    async def test_admin_model_pricing_update_persists_db_override_and_refreshes_registry(self) -> None:
        catalog = {
            "default_text_model": "priced-a",
            "models": [
                {
                    "id": "priced-a",
                    "owned_by": "coincoin",
                    "provider_name": "Google",
                    "provider_model": "gemini-2.5-flash",
                    "capabilities": ["chat/completions", "responses"],
                    "routing_mode": "direct",
                    "delivery_lane": "cpa_gemini",
                    "upstream_model": "gemini-2.5-flash",
                    "upstream_url": "https://gemini.example/v1",
                    "api_key": "gemini-key",
                    "auth_style": "bearer",
                    "price_input_per_million": 100,
                    "price_output_per_million": 200,
                    "billable_sku": "priced-a-text",
                }
            ],
        }
        originals = {
            "model_catalog_json": admin_module._settings.model_catalog_json,
            "model_alias_overrides_path": admin_module._settings.model_alias_overrides_path,
        }

        class _PricingDB:
            def __init__(self) -> None:
                self.rows = []
                self.added = []
                self.commits = 0
                self.execute_count = 0

            async def execute(self, _query):
                self.execute_count += 1
                if self.execute_count in {1, 3}:
                    return _FakeScalarsResult(self.rows)
                if self.execute_count == 2:
                    return _FakeEntityResult(None)
                raise AssertionError("unexpected execute call")

            def add(self, obj):
                self.added.append(obj)
                self.rows.append(obj)

            async def commit(self):
                self.commits += 1

        fake_db = _PricingDB()

        async def fake_get_db():
            yield fake_db

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
                    "/admin/model-pricing/priced-a",
                    json={
                        "model_multiplier": 1.5,
                        "output_multiplier": 2,
                        "cache_read_multiplier": 0.25,
                    },
                )

            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertEqual(payload["id"], "priced-a")
            self.assertTrue(payload["override_active"])
            self.assertEqual(payload["model_multiplier"], 1.5)
            self.assertEqual(payload["output_multiplier"], 2)
            self.assertEqual(payload["cache_read_multiplier"], 0.25)
            self.assertEqual(payload["price_input_per_million"], 150)
            self.assertEqual(payload["price_output_per_million"], 600)

            self.assertEqual(len(fake_db.added), 1)
            self.assertEqual(fake_db.added[0].model_id, "priced-a")
            self.assertEqual(fake_db.added[0].pricing_mode, "multiplier")
            self.assertEqual(fake_db.added[0].price_version, 1)
            self.assertEqual(fake_db.commits, 1)

            resolved = model_registry.resolve_public_model("priced-a", "responses")
            self.assertEqual(resolved.public_model.price_input_per_million, 150)
            self.assertEqual(resolved.public_model.price_output_per_million, 600)
        finally:
            admin_module._settings.model_catalog_json = originals["model_catalog_json"]
            admin_module._settings.model_alias_overrides_path = originals["model_alias_overrides_path"]
            app.dependency_overrides.pop(admin_module.get_db, None)
            model_registry.clear_runtime_pricing_overrides()
            model_registry._initialized = False

    async def test_model_pricing_migration_adds_video_multiplier_to_existing_table(self) -> None:
        class _MigrationConn:
            def __init__(self) -> None:
                self.statements = []

            async def execute(self, statement):
                self.statements.append(str(statement))

        conn = _MigrationConn()
        await main_module._run_migrations(conn)

        self.assertIn(
            "ALTER TABLE coincoin_model_pricing_overrides ADD COLUMN video_multiplier DOUBLE DEFAULT 1",
            conn.statements,
        )

    def test_pricing_payload_tolerates_legacy_model_without_video_fields(self) -> None:
        legacy_model = SimpleNamespace(
            public_id="legacy-priced",
            owned_by="coincoin",
            provider_name="OpenAI",
            provider_model="gpt-5.4",
            delivery_lane="upstream_direct",
            capabilities=("responses",),
            billable_sku="legacy-priced-text",
            base_price_input_per_million=100,
            base_price_output_per_million=200,
            price_input_per_million=100,
            price_output_per_million=200,
            effective_cached_input_per_million=10,
            pricing_mode="explicit_price",
            model_multiplier=1.0,
            output_multiplier=1.0,
            cache_read_multiplier=0.1,
            image_multiplier=1.0,
            price_version=0,
        )

        with patch.object(admin_module.model_registry, "get_public_model", return_value=legacy_model):
            payload = admin_module._pricing_payload("legacy-priced")

        self.assertEqual(payload["id"], "legacy-priced")
        self.assertEqual(payload["base_price_per_video_cents"], 0.0)
        self.assertEqual(payload["price_per_video_cents"], 0.0)
        self.assertEqual(payload["video_multiplier"], 1.0)

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
