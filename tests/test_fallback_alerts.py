import asyncio
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("COINCOIN_DATABASE_URL", "mysql://test@127.0.0.1:3306/test")

import app.fallback_alerts as fallback_alerts
from app.config import settings
from app.fallback_alerts import UpstreamFailureBurstAlert


class UpstreamFailureBurstAlertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._originals = {
            "fallback_alert_webhook_url": settings.fallback_alert_webhook_url,
            "fallback_alert_max_pending_tasks": settings.fallback_alert_max_pending_tasks,
            "upstream_failure_alert_threshold": settings.upstream_failure_alert_threshold,
            "upstream_auth_alert_threshold": settings.upstream_auth_alert_threshold,
            "upstream_failure_alert_window_seconds": settings.upstream_failure_alert_window_seconds,
            "upstream_failure_alert_dedup_seconds": settings.upstream_failure_alert_dedup_seconds,
            "redis_url": settings.redis_url,
        }
        settings.fallback_alert_webhook_url = "https://dingtalk.example/robot"
        settings.fallback_alert_max_pending_tasks = 256
        settings.upstream_failure_alert_threshold = 5
        settings.upstream_auth_alert_threshold = 3
        settings.upstream_failure_alert_window_seconds = 60
        settings.upstream_failure_alert_dedup_seconds = 300
        settings.redis_url = ""
        fallback_alerts.reset_fallback_alert_state()

    def tearDown(self) -> None:
        fallback_alerts.reset_fallback_alert_state()
        for key, value in self._originals.items():
            setattr(settings, key, value)

    def _alert(
        self,
        request_id: str,
        *,
        status_code: int = 502,
        reason: str = "502",
    ) -> UpstreamFailureBurstAlert:
        return UpstreamFailureBurstAlert(
            endpoint="messages",
            model="claude-sonnet-4-6",
            channel_id="ch_sixoner",
            status_code=status_code,
            reason=reason,
            provider_platform="claude_relay",
            request_id=request_id,
        )

    async def test_fifth_availability_failure_within_one_minute_alerts_once(self) -> None:
        with patch.object(fallback_alerts, "notify_upstream_failure_burst", return_value=True) as notify:
            statuses = (502, 503, 500, 504, 529)
            results = [
                await fallback_alerts.record_user_upstream_failure(
                    self._alert(f"ccreq_{index}", status_code=status, reason=str(status)),
                    now=1000 + index,
                )
                for index, status in enumerate(statuses, start=1)
            ]
            sixth = await fallback_alerts.record_user_upstream_failure(
                self._alert("ccreq_6", status_code=503, reason="503"),
                now=1006,
            )

        self.assertEqual(results, [False, False, False, False, True])
        self.assertFalse(sixth)
        notify.assert_called_once()
        sent = notify.call_args.args[0]
        self.assertEqual(sent.category, "availability")
        self.assertEqual(sent.count, 5)
        self.assertEqual(sent.window_seconds, 60)

    async def test_connection_errors_share_availability_counter(self) -> None:
        with patch.object(fallback_alerts, "notify_upstream_failure_burst", return_value=True) as notify:
            for index in range(5):
                await fallback_alerts.record_user_upstream_failure(
                    self._alert(
                        f"ccreq_connection_{index}",
                        status_code=502,
                        reason="upstream_unreachable",
                    ),
                    now=1000 + index,
                )

        self.assertEqual(notify.call_args.args[0].category, "availability")

    async def test_rate_limit_has_separate_capacity_counter(self) -> None:
        with patch.object(fallback_alerts, "notify_upstream_failure_burst", return_value=True) as notify:
            for index in range(5):
                await fallback_alerts.record_user_upstream_failure(
                    self._alert(f"ccreq_429_{index}", status_code=429, reason="429"),
                    now=1000 + index,
                )

        self.assertEqual(notify.call_args.args[0].category, "rate_limit")

    async def test_third_auth_failure_alerts(self) -> None:
        with patch.object(fallback_alerts, "notify_upstream_failure_burst", return_value=True) as notify:
            results = [
                await fallback_alerts.record_user_upstream_failure(
                    self._alert(f"ccreq_auth_{index}", status_code=status, reason=str(status)),
                    now=1000 + index,
                )
                for index, status in enumerate((401, 403, 403), start=1)
            ]

        self.assertEqual(results, [False, False, True])
        self.assertEqual(notify.call_args.args[0].category, "authentication")

    async def test_untracked_client_error_does_not_enter_counter(self) -> None:
        with patch.object(fallback_alerts, "notify_upstream_failure_burst", return_value=True) as notify:
            result = await fallback_alerts.record_user_upstream_failure(
                self._alert("ccreq_400", status_code=400, reason="400"),
                now=1000,
            )

        self.assertFalse(result)
        notify.assert_not_called()

    async def test_failures_outside_window_do_not_trigger(self) -> None:
        with patch.object(fallback_alerts, "notify_upstream_failure_burst", return_value=True) as notify:
            for index, now in enumerate((1000, 1001, 1002, 1003, 1061), start=1):
                await fallback_alerts.record_user_upstream_failure(self._alert(f"ccreq_{index}"), now=now)

        notify.assert_not_called()

    async def test_redis_counter_is_authoritative_when_available(self) -> None:
        settings.redis_url = "redis://alerts.example/0"
        with (
            patch.object(
                fallback_alerts,
                "_record_upstream_failure_redis",
                return_value=(5, True),
            ) as redis_counter,
            patch.object(fallback_alerts, "_record_upstream_failure_local") as local_counter,
            patch.object(fallback_alerts, "notify_upstream_failure_burst", return_value=True) as notify,
        ):
            result = await fallback_alerts.record_user_upstream_failure(self._alert("ccreq_redis"))

        self.assertTrue(result)
        redis_counter.assert_awaited_once()
        local_counter.assert_not_called()
        self.assertEqual(notify.call_args.args[0].count, 5)

    async def test_redis_failure_falls_back_to_process_local_counter(self) -> None:
        settings.redis_url = "redis://alerts.example/0"
        with (
            patch.object(
                fallback_alerts,
                "_record_upstream_failure_redis",
                side_effect=ConnectionError("redis unavailable"),
            ),
            patch.object(
                fallback_alerts,
                "_record_upstream_failure_local",
                return_value=(5, True),
            ) as local_counter,
            patch.object(fallback_alerts, "notify_upstream_failure_burst", return_value=True),
        ):
            result = await fallback_alerts.record_user_upstream_failure(self._alert("ccreq_local_fallback"))

        self.assertTrue(result)
        local_counter.assert_called_once()

    async def test_scheduler_does_not_wait_for_alert_counter(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_counter(alert):
            started.set()
            await release.wait()
            return False

        with patch.object(fallback_alerts, "record_user_upstream_failure", side_effect=slow_counter):
            scheduled = fallback_alerts.schedule_user_upstream_failure(self._alert("ccreq_background"))
            self.assertTrue(scheduled)
            await asyncio.wait_for(started.wait(), timeout=0.1)
            self.assertTrue(fallback_alerts._UPSTREAM_FAILURE_TASKS)
            release.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        self.assertFalse(fallback_alerts._UPSTREAM_FAILURE_TASKS)

    async def test_shutdown_waits_for_inflight_dingtalk_delivery(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_delivery(notification):
            started.set()
            await release.wait()

        notification = fallback_alerts.UpstreamFailureBurstNotification(
            alert=self._alert("ccreq_shutdown"),
            category="availability",
            count=5,
            window_seconds=60,
        )
        with patch.object(fallback_alerts, "_send_upstream_failure_burst_alert", side_effect=slow_delivery):
            self.assertTrue(fallback_alerts.notify_upstream_failure_burst(notification))
            await asyncio.wait_for(started.wait(), timeout=0.1)
            shutdown_task = asyncio.create_task(fallback_alerts.shutdown_fallback_alerts(timeout_seconds=1))
            await asyncio.sleep(0)
            self.assertFalse(shutdown_task.done())
            release.set()
            await shutdown_task

        self.assertFalse(fallback_alerts._UPSTREAM_FAILURE_TASKS)

    async def test_shutdown_drains_delivery_spawned_by_counter_task(self) -> None:
        delivery_started = asyncio.Event()
        release_delivery = asyncio.Event()
        notification = fallback_alerts.UpstreamFailureBurstNotification(
            alert=self._alert("ccreq_nested_shutdown"),
            category="availability",
            count=5,
            window_seconds=60,
        )

        async def counter(alert):
            fallback_alerts.notify_upstream_failure_burst(notification)
            return True

        async def delivery(payload):
            delivery_started.set()
            await release_delivery.wait()

        with (
            patch.object(fallback_alerts, "record_user_upstream_failure", side_effect=counter),
            patch.object(fallback_alerts, "_send_upstream_failure_burst_alert", side_effect=delivery),
        ):
            self.assertTrue(fallback_alerts.schedule_user_upstream_failure(self._alert("ccreq_counter")))
            shutdown_task = asyncio.create_task(fallback_alerts.shutdown_fallback_alerts(timeout_seconds=1))
            await asyncio.wait_for(delivery_started.wait(), timeout=0.1)
            self.assertFalse(shutdown_task.done())
            release_delivery.set()
            await shutdown_task

        self.assertFalse(fallback_alerts._UPSTREAM_FAILURE_TASKS)

    async def test_scheduler_drops_counter_work_when_queue_is_full(self) -> None:
        settings.fallback_alert_max_pending_tasks = 1
        blocker = asyncio.create_task(asyncio.Event().wait())
        fallback_alerts._UPSTREAM_FAILURE_TASKS.add(blocker)
        try:
            scheduled = fallback_alerts.schedule_user_upstream_failure(self._alert("ccreq_overflow"))
            self.assertFalse(scheduled)
        finally:
            fallback_alerts._UPSTREAM_FAILURE_TASKS.discard(blocker)
            blocker.cancel()
            await asyncio.gather(blocker, return_exceptions=True)

    def test_burst_payload_describes_real_user_messages_failures(self) -> None:
        alert = self._alert("ccreq_trace", status_code=503, reason="503")
        payload = fallback_alerts.build_upstream_failure_burst_payload(
            fallback_alerts.UpstreamFailureBurstNotification(
                alert=alert,
                category="availability",
                count=5,
                window_seconds=60,
            )
        )

        content = payload["text"]["content"]
        self.assertIn("真实用户请求", content)
        self.assertIn("5 次", content)
        self.assertIn("HTTP 503", content)
        self.assertIn("ccreq_trace", content)
        self.assertIn("健康检查、监控探针和后台测试不计入", content)


if __name__ == "__main__":
    unittest.main()
