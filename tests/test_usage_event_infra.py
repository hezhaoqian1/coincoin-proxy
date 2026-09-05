import os
import unittest
import asyncio
from unittest.mock import AsyncMock, patch

os.environ.setdefault("COINCOIN_DATABASE_URL", "mysql://test@127.0.0.1:3306/test")

from app.config import settings
from app.quota_lifecycle import QuotaReservationState, _current_reservation, clear_current_quota_reservation
import app.usage_buffer as usage_buffer_module
from app.usage_buffer import UsageBuffer, china_today


class _RecordingSession:
    def __init__(self):
        self.statements = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, statement):
        self.statements.append(statement)

    async def commit(self):
        self.committed = True


class _FailingSession(_RecordingSession):
    async def execute(self, statement):
        raise ConnectionError("database unavailable")


class UsageEventInfraTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._usage_event_shadow_enabled = settings.usage_event_shadow_enabled
        settings.usage_event_shadow_enabled = True
        clear_current_quota_reservation()

    async def asyncTearDown(self):
        clear_current_quota_reservation()
        settings.usage_event_shadow_enabled = self._usage_event_shadow_enabled

    async def test_usage_buffer_shadow_publishes_stable_event_without_skipping_legacy_buffer(self):
        buffer = UsageBuffer()

        with patch("app.usage_events.usage_event_publisher.publish", AsyncMock()) as publish:
            await buffer.add(
                "u_shadow",
                input_tokens=100,
                output_tokens=25,
                cache_read_tokens=10,
                requests=1,
                endpoint="responses",
                model="gpt-5.4",
                api_key_id="k_shadow",
                customer_model_alias="gpt-5.4",
                provider_model="gpt-5.4-upstream",
                billable_sku="gpt-5.4-text",
                upstream_request_id="req_upstream",
                channel_id="ch_primary",
                station_id="st_1",
                station_alias="fast",
                resolved_public_model="gpt-5.4",
                price_version=7,
                price_input_per_million=100,
                price_output_per_million=200,
            )
            await asyncio.sleep(0)

        daily, usage_by_user, request_logs = await buffer.snapshot_and_reset()

        self.assertEqual(daily[("u_shadow", china_today())]["requests"], 1)
        self.assertIn("u_shadow", usage_by_user)
        self.assertEqual(len(request_logs), 1)
        publish.assert_awaited_once()
        event = publish.await_args.args[0]
        self.assertTrue(event.event_id.startswith("uev_"))
        self.assertEqual(event.schema_version, 1)
        self.assertEqual(event.event_type, "usage.recorded")
        self.assertEqual(event.user_id, "u_shadow")
        self.assertEqual(event.api_key_id, "k_shadow")
        self.assertEqual(event.request_log["channel_id"], "ch_primary")
        self.assertEqual(event.request_log["price_version"], 7)
        self.assertEqual(event.usage["input_tokens"], 100)
        self.assertEqual(event.usage["output_tokens"], 25)
        self.assertEqual(event.cost["retail_charge_cents"], request_logs[0]["retail_charge_cents"])

    async def test_usage_event_includes_current_quota_reservation_id(self):
        buffer = UsageBuffer()
        _current_reservation.set(QuotaReservationState(reservation_id="qres_shadow", user_id="u_shadow"))

        with patch("app.usage_buffer.commit_current_quota_reservation", AsyncMock()), patch(
            "app.usage_events.usage_event_publisher.publish", AsyncMock()
        ) as publish:
            await buffer.add("u_shadow", input_tokens=1, output_tokens=1, requests=1)
            await asyncio.sleep(0)

        _, _, request_logs = await buffer.snapshot_and_reset()
        self.assertEqual(request_logs[0]["reservation_id"], "qres_shadow")
        event = publish.await_args.args[0]
        self.assertEqual(event.reservation_id, "qres_shadow")
        self.assertEqual(event.request_log["reservation_id"], "qres_shadow")

    async def test_usage_event_publish_failure_does_not_break_legacy_buffer(self):
        buffer = UsageBuffer()

        with patch("app.usage_events.usage_event_publisher.publish", AsyncMock(side_effect=RuntimeError("redis down"))):
            await buffer.add("u_shadow", input_tokens=10, output_tokens=5, requests=1)
            await asyncio.sleep(0)

        _, usage_by_user, request_logs = await buffer.snapshot_and_reset()
        self.assertIn("u_shadow", usage_by_user)
        self.assertEqual(len(request_logs), 1)

    async def test_request_log_only_records_zero_usage_failure_without_charging_or_aggregating(self):
        buffer = UsageBuffer()

        with patch("app.usage_buffer.commit_current_quota_reservation", AsyncMock()) as commit:
            await buffer.add(
                "u_failed",
                requests=0,
                endpoint="messages",
                model="claude-sonnet-4-6",
                provider_model="claude-sonnet-4-6-upstream",
                status_code=502,
                channel_id="ch_primary",
                upstream_request_id="ccreq_trace|upstream_trace",
                cost_cents_override=0.0,
                request_log_only=True,
            )

        daily, usage_by_user, request_logs = await buffer.snapshot_and_reset()

        self.assertEqual(daily, {})
        self.assertEqual(usage_by_user, {})
        self.assertEqual(len(request_logs), 1)
        self.assertEqual(request_logs[0]["status_code"], 502)
        self.assertEqual(request_logs[0]["cost_cents"], 0)
        self.assertEqual(request_logs[0]["retail_charge_cents"], 0)
        self.assertEqual(request_logs[0]["requests"], 0)
        commit.assert_not_awaited()

    async def test_request_log_only_failure_is_flushed_to_request_log_table(self):
        buffer = UsageBuffer()
        await buffer.add(
            "u_failed",
            requests=0,
            endpoint="messages:stream",
            model="claude-sonnet-4-6",
            provider_model="claude-sonnet-4-6-upstream",
            status_code=503,
            channel_id="ch_primary",
            route_reason="alias_route",
            upstream_request_id="ccreq_trace|upstream_trace",
            cost_cents_override=0.0,
            request_log_only=True,
        )
        session = _RecordingSession()

        with (
            patch.object(usage_buffer_module, "usage_buffer", buffer),
            patch.object(usage_buffer_module, "SessionLocal", return_value=session),
        ):
            await usage_buffer_module.flush_once()

        self.assertTrue(session.committed)
        self.assertEqual(len(session.statements), 1)
        statement = session.statements[0]
        params = statement.compile().params
        values = list(params.values())
        self.assertIn("messages:stream", values)
        self.assertIn(503, values)
        self.assertIn("ch_primary", values)
        self.assertIn("ccreq_trace|upstream_trace", values)
        self.assertIn("ON DUPLICATE KEY UPDATE", str(statement.compile()))

    async def test_failed_flush_requeues_request_log_with_stable_id(self):
        buffer = UsageBuffer()
        await buffer.add(
            "u_failed",
            endpoint="messages",
            model="claude-sonnet-4-6",
            status_code=502,
            upstream_request_id="ccreq_stable",
            cost_cents_override=0.0,
            request_log_only=True,
        )
        stable_id = buffer._request_logs[0]["id"]

        with (
            patch.object(usage_buffer_module, "usage_buffer", buffer),
            patch.object(usage_buffer_module, "SessionLocal", return_value=_FailingSession()),
        ):
            await usage_buffer_module.flush_once()

        daily, usage_by_user, request_logs = await buffer.snapshot_and_reset()
        self.assertEqual(daily, {})
        self.assertEqual(usage_by_user, {})
        self.assertEqual(len(request_logs), 1)
        self.assertEqual(request_logs[0]["id"], stable_id)


if __name__ == "__main__":
    unittest.main()
