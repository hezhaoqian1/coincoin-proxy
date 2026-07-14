import unittest
from datetime import datetime
from types import SimpleNamespace

import httpx

from app.channel_monitoring import (
    AUTO_MONITOR_CREATED_BY,
    desired_route_monitor_specs,
    monitor_model_list,
    reconcile_provider_channel_monitors,
    run_provider_channel_monitor_once,
    serialize_monitor_models,
)
from app.models import (
    ModelChannelRoute,
    ProviderChannel,
    ProviderChannelMonitor,
    ProviderChannelMonitorDailyRollup,
    ProviderChannelMonitorHistory,
)
from app.security import encrypt_api_key


class _FakeScalarOneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeScalars:
    def __init__(self, values):
        self._values = list(values)

    def all(self):
        return list(self._values)


class _FakeScalarsResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return _FakeScalars(self._values)


class _ReconcileDB:
    def __init__(self, *, channels, routes, monitors):
        self.results = [
            _FakeScalarsResult(channels),
            _FakeScalarsResult(routes),
            _FakeScalarsResult(monitors),
        ]
        self.added = []
        self.commits = 0

    async def execute(self, _query):
        return self.results.pop(0)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1


class _FakeDB:
    def __init__(self, *, monitor, channel, daily_rows=None):
        self.monitor = monitor
        self.channel = channel
        self.daily_rows = list(daily_rows or [])
        self.added = []
        self.commits = 0
        self.queries = []

    async def get(self, model, key):
        if model is ProviderChannelMonitor and key == self.monitor.id:
            return self.monitor
        if model is ProviderChannel and key == self.channel.id:
            return self.channel
        return None

    async def execute(self, query):
        self.queries.append(query)
        row = self.daily_rows.pop(0) if self.daily_rows else None
        return _FakeScalarOneResult(row)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


class _FakeClient:
    def __init__(self):
        self.calls = []

    async def get(self, url, headers):
        self.calls.append(("GET", url, dict(headers), None))
        return httpx.Response(200, json={"data": [{"id": "gpt-5.3-codex"}]})

    async def post(self, url, json, headers):
        self.calls.append(("POST", url, dict(headers), dict(json)))
        return httpx.Response(
            200,
            json={
                "id": "resp_monitor",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "pong"}]}],
            },
        )


class _FakeAnthropicClient:
    def __init__(self):
        self.calls = []

    async def get(self, url, headers):
        raise AssertionError(f"Anthropic monitor should not probe /models: {url}")

    async def post(self, url, json, headers):
        self.calls.append(("POST", url, dict(headers), dict(json)))
        return httpx.Response(
            200,
            json={
                "id": "msg_monitor",
                "type": "message",
                "role": "assistant",
                "model": json.get("model"),
                "content": [{"type": "text", "text": "pong"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        )


class ChannelMonitoringTests(unittest.IsolatedAsyncioTestCase):
    def test_desired_route_monitor_specs_group_routes_and_cap_models(self) -> None:
        channel = SimpleNamespace(
            id="ch_openai",
            name="OpenAI Relay",
            channel_type="openai_compatible",
            status="active",
            priority=0,
        )
        routes = [
            SimpleNamespace(id=f"route_{index}", channel_id=channel.id, public_model_id=f"gpt-{index}", upstream_model=f"up-{index}", endpoint="responses", status="active", priority_override=index)
            for index in range(5)
        ]

        specs = desired_route_monitor_specs([channel], routes)

        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].channel_id, "ch_openai")
        self.assertEqual(specs[0].endpoint, "responses")
        self.assertEqual(specs[0].models, ("up-0", "up-1", "up-2"))

    def test_desired_route_monitor_specs_defaults_anthropic_routes_to_chat(self) -> None:
        channel = SimpleNamespace(
            id="ch_claude",
            name="Claude Relay",
            channel_type="anthropic_compatible",
            status="active",
            priority=0,
        )
        route = SimpleNamespace(
            id="route_claude",
            channel_id=channel.id,
            public_model_id="claude-sonnet-5",
            upstream_model="claude-sonnet-5",
            endpoint="",
            status="active",
            priority_override=None,
        )

        specs = desired_route_monitor_specs([channel], [route])

        self.assertEqual(specs[0].endpoint, "chat/completions")

    async def test_reconcile_creates_auto_monitor_for_uncovered_route(self) -> None:
        channel = SimpleNamespace(id="ch_new", name="New Relay", channel_type="openai_compatible", status="active", priority=0)
        route = SimpleNamespace(id="route_new", channel_id=channel.id, public_model_id="gpt-5.6", upstream_model="gpt-5.6", endpoint="responses", status="active", priority_override=None)
        db = _ReconcileDB(channels=[channel], routes=[route], monitors=[])

        result = await reconcile_provider_channel_monitors(db)

        self.assertEqual(result, {"created": 1, "updated": 0, "disabled": 0})
        self.assertEqual(db.commits, 1)
        self.assertEqual(len(db.added), 1)
        monitor = db.added[0]
        self.assertEqual(monitor.channel_id, "ch_new")
        self.assertEqual(monitor.primary_model, "gpt-5.6")
        self.assertEqual(monitor.created_by, AUTO_MONITOR_CREATED_BY)

    async def test_reconcile_reuses_manual_coverage_and_disables_auto_monitor(self) -> None:
        channel = SimpleNamespace(id="ch_existing", name="Existing Relay", channel_type="openai_compatible", status="active", priority=0)
        route = SimpleNamespace(id="route_existing", channel_id=channel.id, public_model_id="gpt-5.6", upstream_model="gpt-5.6", endpoint="responses", status="active", priority_override=None)
        manual = SimpleNamespace(
            id="cmon_manual",
            channel_id=channel.id,
            endpoint="responses",
            primary_model="gpt-5.6",
            extra_models="[]",
            status="active",
            created_by="admin",
        )
        auto = SimpleNamespace(
            id="cma_existing",
            channel_id=channel.id,
            name="Auto",
            endpoint="responses",
            primary_model="gpt-5.6",
            extra_models="[]",
            status="active",
            interval_seconds=300,
            timeout_seconds=30,
            created_by=AUTO_MONITOR_CREATED_BY,
        )
        db = _ReconcileDB(channels=[channel], routes=[route], monitors=[manual, auto])

        result = await reconcile_provider_channel_monitors(db)

        self.assertEqual(result, {"created": 0, "updated": 0, "disabled": 1})
        self.assertEqual(auto.status, "disabled")
        self.assertEqual(manual.status, "active")

    async def test_reconcile_disables_auto_monitor_when_route_is_removed(self) -> None:
        channel = SimpleNamespace(id="ch_removed", name="Removed Relay", channel_type="openai_compatible", status="active", priority=0)
        auto = SimpleNamespace(
            id="cma_removed",
            channel_id=channel.id,
            name="Auto",
            endpoint="responses",
            primary_model="gpt-5.6",
            extra_models="[]",
            status="active",
            interval_seconds=300,
            timeout_seconds=30,
            created_by=AUTO_MONITOR_CREATED_BY,
        )
        db = _ReconcileDB(channels=[channel], routes=[], monitors=[auto])

        result = await reconcile_provider_channel_monitors(db)

        self.assertEqual(result, {"created": 0, "updated": 0, "disabled": 1})
        self.assertEqual(auto.status, "disabled")

    async def test_run_monitor_once_records_history_and_daily_rollup(self) -> None:
        monitor = SimpleNamespace(
            id="cmon_test",
            channel_id="ch_test",
            name="North Star gpt-5.3",
            endpoint="responses",
            primary_model="gpt-5.3-codex",
            extra_models=serialize_monitor_models(["gpt-5.4"]),
            status="active",
            interval_seconds=60,
            timeout_seconds=30,
            last_checked_at=None,
            last_status="",
            last_latency_ms=0,
            last_ping_latency_ms=0,
            last_message="",
        )
        channel = SimpleNamespace(
            id="ch_test",
            base_url="https://sub2api.example",
            encrypted_api_key=encrypt_api_key("sk-test"),
            auth_style="bearer",
        )
        db = _FakeDB(monitor=monitor, channel=channel)
        client = _FakeClient()

        results = await run_provider_channel_monitor_once(db, monitor.id, client=client)

        self.assertEqual([item.model for item in results], ["gpt-5.3-codex", "gpt-5.4"])
        self.assertEqual([item.status for item in results], ["operational", "operational"])
        self.assertEqual(client.calls[0][0], "GET")
        self.assertEqual(client.calls[0][1], "https://sub2api.example/v1/models")
        self.assertEqual(client.calls[0][2]["authorization"], "Bearer sk-test")
        self.assertEqual(client.calls[1][0], "POST")
        self.assertEqual(client.calls[1][1], "https://sub2api.example/v1/responses")
        self.assertEqual(client.calls[1][3]["model"], "gpt-5.3-codex")
        self.assertEqual(client.calls[2][3]["model"], "gpt-5.4")
        self.assertEqual(db.commits, 1)
        self.assertEqual(monitor_model_list(monitor), ["gpt-5.3-codex", "gpt-5.4"])
        self.assertEqual(monitor.last_status, "operational")
        self.assertEqual(monitor.last_message, "ok")
        self.assertIsInstance(monitor.last_checked_at, datetime)
        histories = [obj for obj in db.added if isinstance(obj, ProviderChannelMonitorHistory)]
        daily_rows = [obj for obj in db.added if isinstance(obj, ProviderChannelMonitorDailyRollup)]
        self.assertEqual(len(histories), 2)
        self.assertEqual(len(daily_rows), 2)
        self.assertEqual(daily_rows[0].total_checks, 1)
        self.assertEqual(daily_rows[0].operational_count, 1)

    async def test_anthropic_compatible_monitor_uses_messages_endpoint(self) -> None:
        monitor = SimpleNamespace(
            id="cmon_anthropic",
            channel_id="ch_anthropic",
            name="Claude Fable",
            endpoint="chat/completions",
            primary_model="claude-fable-5",
            extra_models="",
            status="active",
            interval_seconds=60,
            timeout_seconds=30,
            last_checked_at=None,
            last_status="",
            last_latency_ms=0,
            last_ping_latency_ms=0,
            last_message="",
        )
        channel = SimpleNamespace(
            id="ch_anthropic",
            base_url="https://claude-relay.example",
            encrypted_api_key=encrypt_api_key("sk-anthropic"),
            auth_style="x-api-key",
            channel_type="anthropic_compatible",
        )
        db = _FakeDB(monitor=monitor, channel=channel)
        client = _FakeAnthropicClient()

        results = await run_provider_channel_monitor_once(db, monitor.id, client=client)

        self.assertEqual([item.model for item in results], ["claude-fable-5"])
        self.assertEqual(results[0].status, "operational")
        self.assertEqual(client.calls[0][0], "POST")
        self.assertEqual(client.calls[0][1], "https://claude-relay.example/v1/messages")
        self.assertEqual(client.calls[0][2]["x-api-key"], "sk-anthropic")
        self.assertEqual(client.calls[0][2]["anthropic-version"], "2023-06-01")
        self.assertNotIn("authorization", client.calls[0][2])
        self.assertEqual(client.calls[0][3]["model"], "claude-fable-5")
        self.assertEqual(client.calls[0][3]["messages"], [{"role": "user", "content": "ping"}])
        self.assertEqual(monitor.last_status, "operational")

    async def test_claude_code_monitor_uses_claude_code_headers(self) -> None:
        monitor = SimpleNamespace(
            id="cmon_sixoner",
            channel_id="ch_sixoner",
            name="Sixoner Sonnet 5",
            endpoint="chat/completions",
            primary_model="claude-sonnet-5",
            extra_models="",
            status="active",
            interval_seconds=60,
            timeout_seconds=30,
            last_checked_at=None,
            last_status="",
            last_latency_ms=0,
            last_ping_latency_ms=0,
            last_message="",
        )
        channel = SimpleNamespace(
            id="ch_sixoner",
            base_url="https://sub.sixoner.com",
            encrypted_api_key=encrypt_api_key("sk-sixoner"),
            auth_style="bearer",
            channel_type="anthropic_compatible",
            cost_tier="claude-code",
            provider_account_fingerprint="sixoner-claude-code-only",
            notes="Claude Code only upstream",
        )
        db = _FakeDB(monitor=monitor, channel=channel)
        client = _FakeAnthropicClient()

        results = await run_provider_channel_monitor_once(db, monitor.id, client=client)

        self.assertEqual(results[0].status, "operational")
        self.assertEqual(client.calls[0][1], "https://sub.sixoner.com/v1/messages?beta=true")
        self.assertEqual(client.calls[0][2]["authorization"], "Bearer sk-sixoner")
        self.assertIn("claude-code-20250219", client.calls[0][2]["anthropic-beta"])
        self.assertEqual(client.calls[0][2]["anthropic-dangerous-direct-browser-access"], "true")
        self.assertEqual(client.calls[0][2]["user-agent"], "claude-cli/2.1.198 (external, sdk-cli)")
        self.assertEqual(client.calls[0][2]["x-app"], "cli")
        self.assertEqual(client.calls[0][2]["x-claude-code-session-id"], "coincoin-monitor")
        self.assertEqual(client.calls[0][2]["x-stainless-lang"], "js")
        self.assertEqual(client.calls[0][3]["model"], "claude-sonnet-5")
