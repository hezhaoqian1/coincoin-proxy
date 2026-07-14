import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from app.channel_monitoring import (
    AUTO_MONITOR_CREATED_BY,
    claim_due_provider_channel_monitor_ids,
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
        raise AssertionError(f"monitor should not probe /models: {url}")

    async def post(self, url, json, headers):
        self.calls.append(("POST", url, dict(headers), dict(json)))
        return httpx.Response(
            200,
            json={
                "id": "resp_monitor",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "pong"}]}],
            },
        )


class _FakeChatClient:
    def __init__(self):
        self.calls = []

    async def get(self, url, headers):
        raise AssertionError(f"monitor should not probe /models: {url}")

    async def post(self, url, json, headers):
        self.calls.append(("POST", url, dict(headers), dict(json)))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl_monitor",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"}}],
            },
        )


class _FakeInvalidResponseClient:
    def __init__(self):
        self.calls = []

    async def get(self, url, headers):
        raise AssertionError(f"monitor should not probe /models: {url}")

    async def post(self, url, json, headers):
        self.calls.append(("POST", url, dict(headers), dict(json)))
        return httpx.Response(200, json={"id": "resp_monitor", "output": []})


class _FakeRedirectResponseClient:
    def __init__(self):
        self.calls = []

    async def get(self, url, headers):
        raise AssertionError(f"monitor should not probe /models: {url}")

    async def post(self, url, json, headers):
        self.calls.append(("POST", url, dict(headers), dict(json)))
        return httpx.Response(
            302,
            json={
                "id": "resp_redirect",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
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
    def test_desired_route_monitor_specs_select_lowest_priority_highest_weight_then_id(self) -> None:
        channel = SimpleNamespace(
            id="ch_openai",
            name="OpenAI Relay",
            channel_type="openai_compatible",
            status="active",
            priority=5,
            weight=2,
        )
        routes = [
            SimpleNamespace(id="route_low_weight", channel_id=channel.id, public_model_id="gpt-low-weight", upstream_model="up-low-weight", endpoint="responses", status="active", priority_override=0, weight_override=4),
            SimpleNamespace(id="route_b", channel_id=channel.id, public_model_id="gpt-b", upstream_model="up-b", endpoint="chat/completions", status="active", priority_override=0, weight_override=9),
            SimpleNamespace(id="route_a", channel_id=channel.id, public_model_id="gpt-a", upstream_model="up-a", endpoint="responses", status="active", priority_override=0, weight_override=9),
            SimpleNamespace(id="route_channel_defaults", channel_id=channel.id, public_model_id="gpt-default", upstream_model="up-default", endpoint="responses", status="active", priority_override=None, weight_override=None),
        ]

        specs = desired_route_monitor_specs([channel], routes)

        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].channel_id, "ch_openai")
        self.assertEqual(specs[0].endpoint, "responses")
        self.assertEqual(specs[0].models, ("up-a",))

    def test_desired_route_monitor_specs_defaults_anthropic_routes_to_chat(self) -> None:
        channel = SimpleNamespace(
            id="ch_claude",
            name="Claude Relay",
            channel_type="anthropic_compatible",
            status="active",
            priority=0,
            weight=1,
        )
        route = SimpleNamespace(
            id="route_claude",
            channel_id=channel.id,
            public_model_id="claude-sonnet-5",
            upstream_model="claude-sonnet-5",
            endpoint="",
            status="active",
            priority_override=None,
            weight_override=None,
        )

        specs = desired_route_monitor_specs([channel], [route])

        self.assertEqual(specs[0].endpoint, "chat/completions")

    def test_desired_route_monitor_specs_skip_unsupported_probe_endpoints(self) -> None:
        channel = SimpleNamespace(
            id="ch_image",
            name="Image Relay",
            channel_type="openai_compatible",
            status="active",
            priority=0,
            weight=1,
        )
        route = SimpleNamespace(
            id="route_image",
            channel_id=channel.id,
            public_model_id="gpt-image-1",
            upstream_model="gpt-image-1",
            endpoint="images/generations",
            status="active",
            priority_override=None,
            weight_override=None,
        )

        self.assertEqual(desired_route_monitor_specs([channel], [route]), [])

    async def test_reconcile_creates_auto_monitor_for_uncovered_route(self) -> None:
        channel = SimpleNamespace(id="ch_new", name="New Relay", channel_type="openai_compatible", status="active", priority=0)
        channel.weight = 1
        route = SimpleNamespace(id="route_new", channel_id=channel.id, public_model_id="gpt-5.6", upstream_model="gpt-5.6", endpoint="responses", status="active", priority_override=None, weight_override=None)
        db = _ReconcileDB(channels=[channel], routes=[route], monitors=[])

        result = await reconcile_provider_channel_monitors(db)

        self.assertEqual(result, {"created": 1, "updated": 0, "disabled": 0})
        self.assertEqual(db.commits, 1)
        self.assertEqual(len(db.added), 1)
        monitor = db.added[0]
        self.assertEqual(monitor.channel_id, "ch_new")
        self.assertEqual(monitor.primary_model, "gpt-5.6")
        self.assertEqual(monitor.extra_models, "[]")
        self.assertEqual(monitor.created_by, AUTO_MONITOR_CREATED_BY)

    async def test_reconcile_preserves_valid_manual_override_and_disables_auto_monitor(self) -> None:
        channel = SimpleNamespace(id="ch_existing", name="Existing Relay", channel_type="openai_compatible", status="active", priority=0, weight=1)
        routes = [
            SimpleNamespace(id="route_auto", channel_id=channel.id, public_model_id="gpt-5.6", upstream_model="gpt-5.6", endpoint="responses", status="active", priority_override=0, weight_override=10),
            SimpleNamespace(id="route_manual", channel_id=channel.id, public_model_id="gpt-5.5", upstream_model="gpt-5.5", endpoint="chat/completions", status="active", priority_override=5, weight_override=1),
        ]
        manual = SimpleNamespace(
            id="cma_manual_override",
            channel_id=channel.id,
            endpoint="chat/completions",
            primary_model="gpt-5.5",
            extra_models=serialize_monitor_models(["legacy-extra"]),
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
        db = _ReconcileDB(channels=[channel], routes=routes, monitors=[manual, auto])

        result = await reconcile_provider_channel_monitors(db)

        self.assertEqual(result, {"created": 0, "updated": 1, "disabled": 1})
        self.assertEqual(auto.status, "disabled")
        self.assertEqual(manual.status, "active")
        self.assertEqual(manual.endpoint, "chat/completions")
        self.assertEqual(manual.primary_model, "gpt-5.5")
        self.assertEqual(manual.extra_models, "[]")

    async def test_reconcile_disables_invalid_manual_override_without_auto_replacement(self) -> None:
        channel = SimpleNamespace(id="ch_invalid", name="Invalid Relay", channel_type="openai_compatible", status="active", priority=0, weight=1)
        route = SimpleNamespace(id="route_valid", channel_id=channel.id, public_model_id="gpt-5.6", upstream_model="gpt-5.6", endpoint="responses", status="active", priority_override=None, weight_override=None)
        manual = SimpleNamespace(
            id="cmon_invalid",
            channel_id=channel.id,
            endpoint="chat/completions",
            primary_model="removed-model",
            extra_models="[]",
            status="active",
            created_by="admin",
        )
        auto = SimpleNamespace(
            id="cma_invalid",
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

        self.assertEqual(result, {"created": 0, "updated": 0, "disabled": 2})
        self.assertEqual(manual.status, "disabled")
        self.assertEqual(auto.status, "disabled")
        self.assertEqual(db.added, [])

    async def test_reconcile_collapses_legacy_auto_monitors_to_one_primary_model(self) -> None:
        channel = SimpleNamespace(id="ch_legacy", name="Legacy Relay", channel_type="openai_compatible", status="active", priority=0, weight=1)
        routes = [
            SimpleNamespace(id="route_responses", channel_id=channel.id, public_model_id="gpt-5.6", upstream_model="gpt-5.6", endpoint="responses", status="active", priority_override=0, weight_override=10),
            SimpleNamespace(id="route_chat", channel_id=channel.id, public_model_id="gpt-4.9", upstream_model="gpt-4.9", endpoint="chat/completions", status="active", priority_override=5, weight_override=1),
        ]
        matching_auto = SimpleNamespace(
            id="cma_responses",
            channel_id=channel.id,
            name="Old Responses",
            endpoint="responses",
            primary_model="gpt-5.6",
            extra_models=serialize_monitor_models(["gpt-5.5", "gpt-5.4"]),
            status="active",
            interval_seconds=120,
            timeout_seconds=15,
            created_by=AUTO_MONITOR_CREATED_BY,
        )
        redundant_auto = SimpleNamespace(
            id="cma_chat",
            channel_id=channel.id,
            name="Old Chat",
            endpoint="chat/completions",
            primary_model="gpt-4.9",
            extra_models=serialize_monitor_models(["legacy-chat-extra"]),
            status="active",
            interval_seconds=300,
            timeout_seconds=30,
            created_by=AUTO_MONITOR_CREATED_BY,
        )
        db = _ReconcileDB(channels=[channel], routes=routes, monitors=[redundant_auto, matching_auto])

        result = await reconcile_provider_channel_monitors(db)

        self.assertEqual(result, {"created": 0, "updated": 1, "disabled": 1})
        self.assertEqual(matching_auto.status, "active")
        self.assertEqual(matching_auto.primary_model, "gpt-5.6")
        self.assertEqual(matching_auto.endpoint, "responses")
        self.assertEqual(matching_auto.extra_models, "[]")
        self.assertEqual(redundant_auto.status, "disabled")
        self.assertEqual(redundant_auto.extra_models, "[]")

    async def test_reconcile_disables_auto_monitor_when_route_is_removed(self) -> None:
        channel = SimpleNamespace(id="ch_removed", name="Removed Relay", channel_type="openai_compatible", status="active", priority=0, weight=1)
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

    async def test_claim_due_monitors_locks_and_advances_schedule(self) -> None:
        monitor = SimpleNamespace(
            id="cmon_due",
            primary_model="gpt-5.6",
            extra_models="[]",
            claimed_until=None,
            last_checked_at=None,
            interval_seconds=60,
            timeout_seconds=30,
        )
        db = SimpleNamespace(
            execute=AsyncMock(return_value=_FakeScalarsResult([monitor])),
            commit=AsyncMock(),
        )

        monitor_ids = await claim_due_provider_channel_monitor_ids(db, limit=10)

        self.assertEqual(monitor_ids, [monitor.id])
        self.assertIsNone(monitor.last_checked_at)
        self.assertGreater(monitor.claimed_until, datetime.now(UTC).replace(tzinfo=None))
        statement = db.execute.await_args.args[0]
        self.assertTrue(statement._for_update_arg.skip_locked)
        db.commit.assert_awaited_once_with()

    async def test_claim_due_monitors_skips_recent_claim(self) -> None:
        monitor = SimpleNamespace(
            id="cmon_claimed",
            primary_model="gpt-5.6",
            extra_models="[]",
            claimed_until=datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=2),
            last_checked_at=None,
            interval_seconds=60,
            timeout_seconds=30,
        )
        db = SimpleNamespace(
            execute=AsyncMock(return_value=_FakeScalarsResult([monitor])),
            commit=AsyncMock(),
        )

        monitor_ids = await claim_due_provider_channel_monitor_ids(db, limit=10)

        self.assertEqual(monitor_ids, [])
        db.commit.assert_awaited_once_with()

    async def test_run_monitor_once_probes_only_primary_model_and_records_one_result(self) -> None:
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

        self.assertEqual([item.model for item in results], ["gpt-5.3-codex"])
        self.assertEqual([item.status for item in results], ["operational"])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][0], "POST")
        self.assertEqual(client.calls[0][1], "https://sub2api.example/v1/responses")
        self.assertEqual(client.calls[0][2]["authorization"], "Bearer sk-test")
        self.assertEqual(client.calls[0][3]["model"], "gpt-5.3-codex")
        self.assertEqual(client.calls[0][3]["input"], "Reply with OK.")
        self.assertEqual(client.calls[0][3]["max_output_tokens"], 16)
        self.assertFalse(client.calls[0][3]["store"])
        self.assertFalse(client.calls[0][3]["stream"])
        self.assertEqual(db.commits, 1)
        self.assertEqual(monitor_model_list(monitor), ["gpt-5.3-codex", "gpt-5.4"])
        self.assertEqual(monitor.last_status, "operational")
        self.assertEqual(monitor.last_message, "ok")
        self.assertIsInstance(monitor.last_checked_at, datetime)
        histories = [obj for obj in db.added if isinstance(obj, ProviderChannelMonitorHistory)]
        daily_rows = [obj for obj in db.added if isinstance(obj, ProviderChannelMonitorDailyRollup)]
        self.assertEqual(len(histories), 1)
        self.assertEqual(len(daily_rows), 1)
        self.assertEqual(daily_rows[0].total_checks, 1)
        self.assertEqual(daily_rows[0].operational_count, 1)

    async def test_run_monitor_once_rejects_2xx_without_structured_model_output(self) -> None:
        monitor = SimpleNamespace(
            id="cmon_invalid_response",
            channel_id="ch_invalid_response",
            name="Invalid response",
            endpoint="responses",
            primary_model="gpt-5.6",
            extra_models="[]",
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
            id="ch_invalid_response",
            base_url="https://invalid.example",
            encrypted_api_key=encrypt_api_key("sk-test"),
            auth_style="bearer",
            channel_type="openai_compatible",
        )
        db = _FakeDB(monitor=monitor, channel=channel)

        results = await run_provider_channel_monitor_once(db, monitor.id, client=_FakeInvalidResponseClient())

        self.assertEqual(results[0].status, "failed")
        self.assertIn("structured model output", results[0].message)
        self.assertEqual(monitor.last_status, "failed")

    async def test_run_monitor_once_rejects_3xx_with_structured_model_output(self) -> None:
        monitor = SimpleNamespace(
            id="cmon_redirect",
            channel_id="ch_redirect",
            name="Redirect response",
            endpoint="responses",
            primary_model="gpt-5.6",
            extra_models="[]",
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
            id="ch_redirect",
            base_url="https://redirect.example",
            encrypted_api_key=encrypt_api_key("sk-test"),
            auth_style="bearer",
            channel_type="openai_compatible",
        )
        db = _FakeDB(monitor=monitor, channel=channel)

        results = await run_provider_channel_monitor_once(db, monitor.id, client=_FakeRedirectResponseClient())

        self.assertEqual(results[0].status, "error")
        self.assertEqual(results[0].message, "HTTP 302")
        self.assertEqual(monitor.last_status, "error")

    async def test_chat_monitor_uses_minimal_generation_request(self) -> None:
        monitor = SimpleNamespace(
            id="cmon_chat",
            channel_id="ch_chat",
            name="Chat Relay",
            endpoint="chat/completions",
            primary_model="gpt-chat",
            extra_models="[]",
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
            id="ch_chat",
            base_url="https://chat.example",
            encrypted_api_key=encrypt_api_key("sk-chat"),
            auth_style="bearer",
            channel_type="openai_compatible",
        )
        db = _FakeDB(monitor=monitor, channel=channel)
        client = _FakeChatClient()

        results = await run_provider_channel_monitor_once(db, monitor.id, client=client)

        self.assertEqual(results[0].status, "operational")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][1], "https://chat.example/v1/chat/completions")
        self.assertEqual(client.calls[0][3]["messages"], [{"role": "user", "content": "Reply with OK."}])
        self.assertEqual(client.calls[0][3]["max_tokens"], 8)
        self.assertFalse(client.calls[0][3]["stream"])

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
        self.assertEqual(client.calls[0][3]["messages"], [{"role": "user", "content": "Reply with OK."}])
        self.assertEqual(client.calls[0][3]["max_tokens"], 8)
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
