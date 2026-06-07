import unittest
import asyncio
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.quota_lifecycle import QuotaReservationState, _current_reservation, clear_current_quota_reservation
from app.usage_buffer import UsageBuffer, china_today


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


if __name__ == "__main__":
    unittest.main()
