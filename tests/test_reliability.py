import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

os.environ.setdefault("COINCOIN_DATABASE_URL", "mysql://test@127.0.0.1:3306/test")

from app.main import app
import app.admin as admin_module
from app.config import settings
from app.reliability import (
    assemble_reliability_overview,
    get_cached_reliability_overview,
    invalidate_reliability_cache,
)


class ReliabilityOverviewTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        settings.admin_token = ""
        invalidate_reliability_cache()

    def test_assemble_overview_includes_unconfigured_pending_and_degraded_channels(self) -> None:
        now = datetime(2026, 7, 14, 10, 0, 0)
        channels = [
            SimpleNamespace(
                id="ch_empty",
                name="New Channel",
                provider_platform="sub2api",
                channel_type="openai_compatible",
                status="active",
                priority=2,
                weight=1,
                base_url="https://new.example/v1",
            ),
            SimpleNamespace(
                id="ch_pending",
                name="Pending Route",
                provider_platform="sub2api",
                channel_type="openai_compatible",
                status="active",
                priority=0,
                weight=1,
                base_url="https://pending.example/v1",
            ),
            SimpleNamespace(
                id="ch_degraded",
                name="Fallback Route",
                provider_platform="sub2api",
                channel_type="openai_compatible",
                status="active",
                priority=1,
                weight=1,
                base_url="https://fallback.example/v1",
            ),
        ]
        routes = [
            SimpleNamespace(
                id="route_pending",
                public_model_id="gpt-5.6",
                endpoint="responses",
                channel_id="ch_pending",
                upstream_model="gpt-5.6",
                priority_override=None,
                weight_override=None,
                transform_profile="openai_compatible",
                status="active",
            ),
            SimpleNamespace(
                id="route_degraded",
                public_model_id="gpt-5.5",
                endpoint="responses",
                channel_id="ch_degraded",
                upstream_model="gpt-5.5",
                priority_override=None,
                weight_override=None,
                transform_profile="openai_compatible",
                status="active",
            ),
        ]
        monitors = [
            SimpleNamespace(
                id="cmon_degraded",
                channel_id="ch_degraded",
                endpoint="responses",
                primary_model="gpt-5.5",
                extra_models="[]",
                status="active",
                last_checked_at=now - timedelta(seconds=30),
                last_status="failed",
                last_latency_ms=30_000,
                last_ping_latency_ms=100,
                last_message="HTTP 503",
            )
        ]
        traffic_rows = [
            SimpleNamespace(
                public_model_id="gpt-5.5",
                channel_id="ch_degraded",
                requests=10,
                success_requests=9,
                failed_requests=1,
                fallback_requests=3,
                avg_latency_ms=4200,
                max_latency_ms=9000,
                last_seen_at=now - timedelta(seconds=5),
            )
        ]

        payload = assemble_reliability_overview(
            channels=channels,
            routes=routes,
            runtime_states=[],
            monitors=monitors,
            traffic_rows=traffic_rows,
            recent_failures=[],
            now=now,
        )

        by_channel = {item["id"]: item for item in payload["channels"]}
        self.assertEqual(by_channel["ch_empty"]["health_status"], "unconfigured")
        self.assertEqual(by_channel["ch_pending"]["health_status"], "pending")
        self.assertEqual(by_channel["ch_degraded"]["health_status"], "failed")
        self.assertEqual(by_channel["ch_degraded"]["fallback_requests_5m"], 3)

        by_model = {item["public_model_id"]: item for item in payload["models"]}
        self.assertEqual(by_model["gpt-5.6"]["health_status"], "operational")
        self.assertEqual(by_model["gpt-5.5"]["health_status"], "degraded")
        self.assertEqual(by_model["gpt-5.5"]["fallback_rate_5m"], 0.3)
        self.assertEqual(payload["overall"]["health_status"], "failed")
        self.assertGreaterEqual(payload["overall"]["active_incidents"], 1)

    def test_failed_representative_probe_affects_channel_not_public_model_route(self) -> None:
        now = datetime(2026, 7, 15, 10, 0, 0)
        channel = SimpleNamespace(
            id="ch_probe",
            name="Probe Channel",
            provider_platform="sub2api",
            channel_type="openai_compatible",
            status="active",
            priority=0,
            weight=1,
        )
        route = SimpleNamespace(
            id="route_probe",
            public_model_id="gpt-public",
            endpoint="responses",
            channel_id=channel.id,
            upstream_model="gpt-upstream",
            priority_override=None,
            weight_override=None,
            status="active",
        )
        monitor = SimpleNamespace(
            id="monitor_probe",
            channel_id=channel.id,
            endpoint="responses",
            primary_model="gpt-upstream",
            status="active",
            created_by="route-reconciler",
            last_status="failed",
            last_message="HTTP 503",
            last_checked_at=now,
        )

        payload = assemble_reliability_overview(
            channels=[channel],
            routes=[route],
            runtime_states=[],
            monitors=[monitor],
            traffic_rows=[],
            recent_failures=[],
            now=now,
        )

        self.assertEqual(payload["channels"][0]["health_status"], "failed")
        self.assertEqual(payload["models"][0]["health_status"], "operational")
        self.assertEqual(payload["models"][0]["routes"][0]["health_status"], "operational")

    def test_actual_runtime_cooldown_affects_public_model_route_health(self) -> None:
        now = datetime(2026, 7, 15, 10, 0, 0)
        channel = SimpleNamespace(
            id="ch_cooling",
            name="Cooling Channel",
            provider_platform="sub2api",
            channel_type="openai_compatible",
            status="active",
            priority=0,
            weight=1,
        )
        route = SimpleNamespace(
            id="route_cooling",
            public_model_id="gpt-public",
            endpoint="responses",
            channel_id=channel.id,
            upstream_model="gpt-upstream",
            priority_override=None,
            weight_override=None,
            status="active",
        )
        runtime = SimpleNamespace(channel_id=channel.id, cooldown_until=now + timedelta(seconds=30))

        payload = assemble_reliability_overview(
            channels=[channel],
            routes=[route],
            runtime_states=[runtime],
            monitors=[],
            traffic_rows=[],
            recent_failures=[],
            now=now,
        )

        self.assertEqual(payload["models"][0]["health_status"], "cooling")
        self.assertEqual(payload["models"][0]["routes"][0]["health_status"], "cooling")

    def test_high_real_traffic_latency_degrades_route_and_model_without_probe_failure(self) -> None:
        now = datetime(2026, 7, 15, 10, 0, 0)
        channel = SimpleNamespace(id="ch_slow", name="Slow", provider_platform="sub2api", channel_type="openai_compatible", status="active", priority=0, weight=1)
        route = SimpleNamespace(id="route_slow", public_model_id="gpt-slow", endpoint="responses", channel_id=channel.id, upstream_model="gpt-slow", priority_override=None, weight_override=None, status="active")
        monitor = SimpleNamespace(id="monitor_slow", channel_id=channel.id, endpoint="responses", primary_model="gpt-slow", status="active", created_by="route-reconciler", last_status="operational", last_message="ok", last_checked_at=now)
        traffic = SimpleNamespace(public_model_id="gpt-slow", channel_id=channel.id, requests=4, success_requests=4, failed_requests=0, fallback_requests=0, avg_latency_ms=30_000, max_latency_ms=34_000, last_seen_at=now)

        payload = assemble_reliability_overview(
            channels=[channel],
            routes=[route],
            runtime_states=[],
            monitors=[monitor],
            traffic_rows=[traffic],
            recent_failures=[],
            now=now,
        )

        self.assertEqual(payload["channels"][0]["monitor_status"], "operational")
        self.assertEqual(payload["models"][0]["health_status"], "degraded")
        self.assertEqual(payload["models"][0]["routes"][0]["health_status"], "degraded")

    def test_real_traffic_latency_below_slow_threshold_remains_operational(self) -> None:
        now = datetime(2026, 7, 15, 10, 0, 0)
        channel = SimpleNamespace(id="ch_fast", name="Fast", provider_platform="sub2api", channel_type="openai_compatible", status="active", priority=0, weight=1)
        route = SimpleNamespace(id="route_fast", public_model_id="gpt-fast", endpoint="responses", channel_id=channel.id, upstream_model="gpt-fast", priority_override=None, weight_override=None, status="active")
        traffic = SimpleNamespace(public_model_id="gpt-fast", channel_id=channel.id, requests=4, success_requests=4, failed_requests=0, fallback_requests=0, avg_latency_ms=29_999, max_latency_ms=45_000, last_seen_at=now)

        payload = assemble_reliability_overview(
            channels=[channel],
            routes=[route],
            runtime_states=[],
            monitors=[],
            traffic_rows=[traffic],
            recent_failures=[],
            now=now,
        )

        self.assertEqual(payload["models"][0]["health_status"], "operational")
        self.assertEqual(payload["models"][0]["routes"][0]["health_status"], "operational")

    def test_overall_summary_is_channel_first(self) -> None:
        now = datetime(2026, 7, 15, 10, 0, 0)
        channels = [
            SimpleNamespace(id="ch_ok", name="OK", provider_platform="sub2api", channel_type="openai_compatible", status="active", priority=0, weight=1),
            SimpleNamespace(id="ch_bad", name="Bad", provider_platform="sub2api", channel_type="openai_compatible", status="active", priority=1, weight=1),
        ]
        routes = [
            SimpleNamespace(id="route_ok", public_model_id="gpt-ok", endpoint="responses", channel_id="ch_ok", upstream_model="gpt-ok", priority_override=None, weight_override=None, status="active"),
            SimpleNamespace(id="route_bad", public_model_id="gpt-bad", endpoint="responses", channel_id="ch_bad", upstream_model="gpt-bad", priority_override=None, weight_override=None, status="active"),
        ]
        monitors = [
            SimpleNamespace(id="monitor_ok", channel_id="ch_ok", endpoint="responses", primary_model="gpt-ok", status="active", created_by="route-reconciler", last_status="operational", last_message="ok", last_checked_at=now),
            SimpleNamespace(id="monitor_bad", channel_id="ch_bad", endpoint="responses", primary_model="gpt-bad", status="active", created_by="admin", last_status="failed", last_message="HTTP 503", last_checked_at=now),
        ]

        payload = assemble_reliability_overview(
            channels=channels,
            routes=routes,
            runtime_states=[],
            monitors=monitors,
            traffic_rows=[],
            recent_failures=[],
            now=now,
        )

        self.assertEqual(payload["overall"]["health_status"], "failed")
        self.assertEqual(payload["overall"]["channels_total"], 2)
        self.assertEqual(payload["overall"]["channels_operational"], 1)
        self.assertEqual(payload["overall"]["channels_affected"], 1)
        self.assertTrue(all(item["health_status"] == "operational" for item in payload["models"]))

    def test_overall_status_ignores_disabled_channels_when_active_channels_are_healthy(self) -> None:
        now = datetime(2026, 7, 15, 10, 0, 0)
        channels = [
            SimpleNamespace(id="ch_active", name="Active", provider_platform="sub2api", channel_type="openai_compatible", status="active", priority=0, weight=1),
            SimpleNamespace(id="ch_disabled", name="Disabled", provider_platform="sub2api", channel_type="openai_compatible", status="disabled", priority=1, weight=1),
        ]
        route = SimpleNamespace(id="route_active", public_model_id="gpt-active", endpoint="responses", channel_id="ch_active", upstream_model="gpt-active", priority_override=None, weight_override=None, status="active")
        monitor = SimpleNamespace(id="monitor_active", channel_id="ch_active", endpoint="responses", primary_model="gpt-active", status="active", created_by="route-reconciler", last_status="operational", last_message="ok", last_checked_at=now)

        payload = assemble_reliability_overview(
            channels=channels,
            routes=[route],
            runtime_states=[],
            monitors=[monitor],
            traffic_rows=[],
            recent_failures=[],
            now=now,
        )

        self.assertEqual(payload["overall"]["health_status"], "operational")
        self.assertEqual(payload["overall"]["channels_total"], 2)

    def test_channel_row_exposes_representative_monitor_target(self) -> None:
        now = datetime(2026, 7, 15, 10, 0, 0)
        channel = SimpleNamespace(id="ch_target", name="Target", provider_platform="sub2api", channel_type="openai_compatible", status="active", priority=0, weight=1)
        route = SimpleNamespace(id="route_target", public_model_id="gpt-public", endpoint="responses", channel_id=channel.id, upstream_model="gpt-upstream", priority_override=None, weight_override=None, status="active")
        monitor = SimpleNamespace(id="monitor_target", channel_id=channel.id, endpoint="responses", primary_model="gpt-upstream", status="active", created_by="route-reconciler", last_status="operational", last_message="ok", last_checked_at=now)

        payload = assemble_reliability_overview(
            channels=[channel],
            routes=[route],
            runtime_states=[],
            monitors=[monitor],
            traffic_rows=[],
            recent_failures=[],
            now=now,
        )

        row = payload["channels"][0]
        self.assertEqual(row["monitor_model"], "gpt-upstream")
        self.assertEqual(row["monitor_endpoint"], "responses")
        self.assertEqual(row["monitor_mode"], "auto")

    async def test_cached_overview_builds_once_inside_ttl(self) -> None:
        expected = {"generated_at": "2026-07-14T10:00:00", "overall": {"health_status": "operational"}}
        builder = AsyncMock(return_value=expected)

        with patch("app.reliability.build_reliability_overview", builder):
            first = await get_cached_reliability_overview(object())
            second = await get_cached_reliability_overview(object())

        self.assertEqual(first, expected)
        self.assertEqual(second, expected)
        builder.assert_awaited_once()

    async def test_manual_monitor_run_invalidates_reliability_cache(self) -> None:
        db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(id="monitor-1")))
        result = SimpleNamespace(
            model="gpt-5.5",
            status="operational",
            latency_ms=1200,
            ping_latency_ms=80,
            status_code=200,
            message="ok",
            checked_at=datetime(2026, 7, 14, 10, 0, 0),
        )

        with (
            patch.object(admin_module, "run_provider_channel_monitor_once", AsyncMock(return_value=[result])),
            patch.object(admin_module, "invalidate_reliability_cache") as invalidate,
        ):
            response = await admin_module.run_provider_channel_monitor_now("monitor-1", db)

        self.assertEqual(response.status_code, 200)
        invalidate.assert_called_once_with()

    def test_assemble_overview_aggregates_same_model_across_channels(self) -> None:
        now = datetime(2026, 7, 14, 10, 0, 0)
        channels = [
            SimpleNamespace(id="ch_a", name="A", provider_platform="sub2api", channel_type="openai_compatible", status="active", priority=0, weight=1, base_url="https://a.example"),
            SimpleNamespace(id="ch_b", name="B", provider_platform="sub2api", channel_type="openai_compatible", status="active", priority=1, weight=1, base_url="https://b.example"),
        ]
        routes = [
            SimpleNamespace(id="route_a", public_model_id="gpt-5.6", endpoint="responses", channel_id="ch_a", upstream_model="gpt-5.6", priority_override=None, weight_override=None, transform_profile="openai_compatible", status="active"),
            SimpleNamespace(id="route_b", public_model_id="gpt-5.6", endpoint="responses", channel_id="ch_b", upstream_model="gpt-5.6", priority_override=None, weight_override=None, transform_profile="openai_compatible", status="active"),
        ]
        traffic_rows = [
            SimpleNamespace(public_model_id="gpt-5.6", channel_id="ch_a", requests=8, success_requests=8, failed_requests=0, fallback_requests=0, avg_latency_ms=1000, max_latency_ms=1500, last_seen_at=now - timedelta(seconds=20)),
            SimpleNamespace(public_model_id="gpt-5.6", channel_id="ch_b", requests=2, success_requests=1, failed_requests=1, fallback_requests=2, avg_latency_ms=3000, max_latency_ms=4000, last_seen_at=now - timedelta(seconds=5)),
        ]

        payload = assemble_reliability_overview(
            channels=channels,
            routes=routes,
            runtime_states=[],
            monitors=[],
            traffic_rows=traffic_rows,
            recent_failures=[],
            now=now,
        )

        model = payload["models"][0]
        self.assertEqual(model["requests_5m"], 10)
        self.assertEqual(model["failed_requests_5m"], 1)
        self.assertEqual(model["fallback_requests_5m"], 2)
        self.assertEqual(model["fallback_rate_5m"], 0.2)
        self.assertEqual(model["avg_latency_ms_5m"], 1400)
        self.assertEqual(model["max_latency_ms_5m"], 4000)

    def test_channel_action_uses_worst_active_monitor(self) -> None:
        now = datetime(2026, 7, 14, 10, 0, 0)
        channel = SimpleNamespace(
            id="ch_multi",
            name="Multi Endpoint",
            provider_platform="sub2api",
            channel_type="openai_compatible",
            status="active",
            priority=0,
            weight=1,
        )
        route = SimpleNamespace(
            id="route_multi",
            public_model_id="gpt-5.6",
            endpoint="responses",
            channel_id=channel.id,
            upstream_model="gpt-5.6",
            priority_override=None,
            weight_override=None,
            status="active",
        )
        monitors = [
            SimpleNamespace(id="monitor-ok", channel_id=channel.id, status="active", last_status="operational", last_message="ok", last_checked_at=now),
            SimpleNamespace(id="monitor-failed", channel_id=channel.id, status="active", last_status="failed", last_message="HTTP 503", last_checked_at=now),
        ]

        payload = assemble_reliability_overview(
            channels=[channel],
            routes=[route],
            runtime_states=[],
            monitors=monitors,
            traffic_rows=[],
            recent_failures=[],
            now=now,
        )

        self.assertEqual(payload["channels"][0]["monitor_id"], "monitor-failed")
        self.assertEqual(payload["channels"][0]["monitor_message"], "HTTP 503")

    async def test_admin_reliability_overview_requires_admin_token(self) -> None:
        settings.admin_token = "admin-secret"
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/admin/reliability/overview")

        self.assertEqual(response.status_code, 401, response.text)

    async def test_admin_reliability_overview_returns_cached_payload(self) -> None:
        settings.admin_token = "admin-secret"
        expected = {
            "generated_at": "2026-07-14T10:00:00",
            "cache_ttl_seconds": 10,
            "overall": {"health_status": "operational"},
            "models": [],
            "channels": [],
            "incidents": [],
            "recent_failures": [],
        }

        with patch("app.reliability.get_cached_reliability_overview", AsyncMock(return_value=expected)):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.get(
                    "/admin/reliability/overview",
                    headers={"authorization": "Bearer admin-secret"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), expected)


if __name__ == "__main__":
    unittest.main()
