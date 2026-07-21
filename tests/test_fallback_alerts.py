import asyncio
import logging
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

os.environ.setdefault("COINCOIN_DATABASE_URL", "mysql://test@127.0.0.1:3306/test")

import app.fallback_alerts as fallback_alerts
from app.config import settings
from app.fallback_alerts import UpstreamFailureBurstAlert


class UpstreamFailureBurstAlertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._originals = {
            "fallback_alert_webhook_url": settings.fallback_alert_webhook_url,
            "fallback_alert_enabled": settings.fallback_alert_enabled,
            "fallback_alert_keyword": settings.fallback_alert_keyword,
            "fallback_alert_max_pending_tasks": settings.fallback_alert_max_pending_tasks,
            "upstream_failure_alert_threshold": settings.upstream_failure_alert_threshold,
            "upstream_auth_alert_threshold": settings.upstream_auth_alert_threshold,
            "upstream_failure_alert_window_seconds": settings.upstream_failure_alert_window_seconds,
            "upstream_failure_alert_dedup_seconds": settings.upstream_failure_alert_dedup_seconds,
            "redis_url": settings.redis_url,
        }
        settings.fallback_alert_webhook_url = (
            "https://oapi.dingtalk.com/robot/send?access_token=environment-token"
        )
        settings.fallback_alert_enabled = True
        settings.fallback_alert_keyword = "CoinCoin"
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
        with patch.object(fallback_alerts, "_send_upstream_failure_burst_alert", AsyncMock(return_value=True)) as notify:
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
        with patch.object(fallback_alerts, "_send_upstream_failure_burst_alert", AsyncMock(return_value=True)) as notify:
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
        with patch.object(fallback_alerts, "_send_upstream_failure_burst_alert", AsyncMock(return_value=True)) as notify:
            for index in range(5):
                await fallback_alerts.record_user_upstream_failure(
                    self._alert(f"ccreq_429_{index}", status_code=429, reason="429"),
                    now=1000 + index,
                )

        self.assertEqual(notify.call_args.args[0].category, "rate_limit")

    async def test_third_auth_failure_alerts(self) -> None:
        with patch.object(fallback_alerts, "_send_upstream_failure_burst_alert", AsyncMock(return_value=True)) as notify:
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
        with patch.object(fallback_alerts, "_send_upstream_failure_burst_alert", AsyncMock(return_value=True)) as notify:
            result = await fallback_alerts.record_user_upstream_failure(
                self._alert("ccreq_400", status_code=400, reason="400"),
                now=1000,
            )

        self.assertFalse(result)
        notify.assert_not_called()

    async def test_failures_outside_window_do_not_trigger(self) -> None:
        with patch.object(fallback_alerts, "_send_upstream_failure_burst_alert", AsyncMock(return_value=True)) as notify:
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
            patch.object(fallback_alerts, "_send_upstream_failure_burst_alert", AsyncMock(return_value=True)) as notify,
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
            patch.object(fallback_alerts, "_send_upstream_failure_burst_alert", AsyncMock(return_value=True)),
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

    async def test_runtime_policy_override_disables_scheduling_without_io(self) -> None:
        fallback_alerts.set_runtime_alert_settings({"fallback_alert_enabled": "false"})

        with (
            patch.object(asyncio, "create_task") as create_task,
            patch.object(fallback_alerts, "record_user_upstream_failure", AsyncMock()) as record,
        ):
            scheduled = fallback_alerts.schedule_user_upstream_failure(self._alert("ccreq_disabled"))

        self.assertFalse(scheduled)
        create_task.assert_not_called()
        record.assert_not_called()

    def test_runtime_policy_database_values_override_environment_defaults(self) -> None:
        settings.upstream_failure_alert_threshold = 5
        settings.upstream_auth_alert_threshold = 3
        fallback_alerts.set_runtime_alert_settings(
            {
                "upstream_failure_alert_threshold": "9",
                "upstream_auth_alert_threshold": "4",
                "upstream_failure_alert_window_seconds": "120",
                "upstream_failure_alert_dedup_seconds": "600",
                "fallback_alert_max_pending_tasks": "32",
            }
        )

        policy = fallback_alerts.current_alert_policy()

        self.assertEqual(policy.availability_threshold, 9)
        self.assertEqual(policy.authentication_threshold, 4)
        self.assertEqual(policy.window_seconds, 120)
        self.assertEqual(policy.dedup_seconds, 600)
        self.assertEqual(policy.max_pending_tasks, 32)

    def test_runtime_webhook_database_value_overrides_environment_default(self) -> None:
        settings.fallback_alert_webhook_url = "https://environment.example/webhook"
        runtime_url = "https://runtime.example/webhook"

        fallback_alerts.set_runtime_alert_settings(
            {"fallback_alert_webhook_url": runtime_url}
        )

        self.assertEqual(fallback_alerts.current_alert_webhook_url(), runtime_url)

    def test_runtime_empty_webhook_shadows_environment_default(self) -> None:
        settings.fallback_alert_webhook_url = "https://environment.example/webhook"

        fallback_alerts.set_runtime_alert_settings(
            {"fallback_alert_webhook_url": ""}
        )

        self.assertEqual(fallback_alerts.current_alert_webhook_url(), "")
        with patch.object(asyncio, "create_task") as create_task:
            scheduled = fallback_alerts.schedule_user_upstream_failure(
                self._alert("ccreq_runtime_disabled")
            )
        self.assertFalse(scheduled)
        create_task.assert_not_called()

    async def test_malformed_runtime_webhook_is_never_scheduled_or_sent(self) -> None:
        malformed_url = "https://internal.example/robot/send?access_token=legacy-token"
        fallback_alerts.set_runtime_alert_settings(
            {"fallback_alert_webhook_url": malformed_url}
        )
        response = SimpleNamespace(status_code=200, json=lambda: {"errcode": 0})
        client = AsyncMock()
        client.post.return_value = response
        client.__aenter__.return_value = client
        client.__aexit__.return_value = False
        fallback = fallback_alerts.FallbackExhaustedAlert(
            endpoint="messages",
            model="claude-sonnet-4-6",
            status_code=503,
            reason="503",
            route_reason="channel_fallback:all_failed",
            route_attempt=2,
        )
        notification = fallback_alerts.UpstreamFailureBurstNotification(
            alert=self._alert("ccreq_malformed_runtime"),
            category="availability",
            count=5,
            window_seconds=60,
        )

        with (
            patch.object(asyncio, "create_task") as create_task,
            patch.object(
                fallback_alerts,
                "record_user_upstream_failure",
                new=lambda alert: None,
            ),
        ):
            scheduled = fallback_alerts.schedule_user_upstream_failure(
                self._alert("ccreq_malformed_schedule")
            )

        with (
            patch.object(
                fallback_alerts,
                "_dingtalk_http_client",
                return_value=client,
            ) as client_factory,
            patch.object(
                fallback_alerts,
                "create_alert_event",
                AsyncMock(return_value=None),
            ) as create_event,
        ):
            fallback_result = await fallback_alerts._send_dingtalk_alert(fallback)
            burst_result = await fallback_alerts._send_upstream_failure_burst_alert(
                notification
            )
            test_result = await fallback_alerts.send_dingtalk_configuration_test()

        self.assertEqual(fallback_alerts.current_alert_webhook_url(), malformed_url)
        self.assertFalse(scheduled)
        create_task.assert_not_called()
        self.assertFalse(fallback_result)
        self.assertFalse(burst_result)
        self.assertEqual(test_result, {"sent": False, "event_id": None})
        client_factory.assert_not_called()
        client.post.assert_not_awaited()
        create_event.assert_not_awaited()
        self.assertEqual(fallback_alerts.current_sendable_alert_webhook_url(), "")

    def test_malformed_environment_webhook_is_not_scheduled(self) -> None:
        fallback_alerts.reset_fallback_alert_state()
        settings.fallback_alert_webhook_url = (
            "https://internal.example/robot/send?access_token=environment-token"
        )

        with (
            patch.object(asyncio, "create_task") as create_task,
            patch.object(
                fallback_alerts,
                "record_user_upstream_failure",
                new=lambda alert: None,
            ),
        ):
            scheduled = fallback_alerts.schedule_user_upstream_failure(
                self._alert("ccreq_malformed_environment")
            )

        self.assertEqual(
            fallback_alerts.current_alert_webhook_url(),
            settings.fallback_alert_webhook_url,
        )
        self.assertFalse(scheduled)
        create_task.assert_not_called()

    async def test_sender_uses_runtime_webhook_without_database_lookup(self) -> None:
        notification = fallback_alerts.UpstreamFailureBurstNotification(
            alert=self._alert("ccreq_runtime_webhook"),
            category="availability",
            count=5,
            window_seconds=60,
        )
        runtime_url = "https://oapi.dingtalk.com/robot/send?access_token=runtime-token"
        fallback_alerts.set_runtime_alert_settings(
            {"fallback_alert_webhook_url": runtime_url}
        )
        response = SimpleNamespace(status_code=200, json=lambda: {"errcode": 0})
        client = AsyncMock()
        client.post.return_value = response
        client.__aenter__.return_value = client
        client.__aexit__.return_value = False

        with (
            patch.object(fallback_alerts.httpx, "AsyncClient", return_value=client),
            patch.object(fallback_alerts, "create_alert_event", AsyncMock(return_value=None)),
        ):
            delivered = await fallback_alerts._send_upstream_failure_burst_alert(notification)

        self.assertTrue(delivered)
        client.post.assert_awaited_once()
        self.assertEqual(client.post.call_args.args[0], runtime_url)

    async def test_successful_burst_delivery_records_sanitized_event_lifecycle(self) -> None:
        notification = fallback_alerts.UpstreamFailureBurstNotification(
            alert=self._alert("ccreq_delivery"),
            category="availability",
            count=5,
            window_seconds=60,
        )
        response = SimpleNamespace(status_code=200, json=lambda: {"errcode": 0, "errmsg": "ok"})
        client = AsyncMock()
        client.post.return_value = response
        client.__aenter__.return_value = client
        client.__aexit__.return_value = False

        with (
            patch.object(fallback_alerts.httpx, "AsyncClient", return_value=client),
            patch.object(fallback_alerts, "create_alert_event", AsyncMock(return_value="alert_123")) as create_event,
            patch.object(fallback_alerts, "complete_alert_event", AsyncMock()) as complete_event,
        ):
            delivered = await fallback_alerts._send_upstream_failure_burst_alert(notification)

        self.assertTrue(delivered)
        create_event.assert_awaited_once()
        fields = create_event.call_args.kwargs
        self.assertEqual(fields["request_id"], "ccreq_delivery")
        self.assertEqual(fields["delivery_status"], "pending")
        self.assertNotIn("webhook", fields)
        complete_event.assert_awaited_once_with(
            "alert_123",
            delivery_status="sent",
            response_status=200,
            error_summary="",
        )

    async def test_failed_burst_delivery_records_status_without_raw_body(self) -> None:
        notification = fallback_alerts.UpstreamFailureBurstNotification(
            alert=self._alert("ccreq_failed_delivery"),
            category="availability",
            count=5,
            window_seconds=60,
        )
        response = SimpleNamespace(
            status_code=502,
            text="cloudflare secret diagnostic body",
            json=lambda: {"errcode": 500, "errmsg": "sensitive upstream response"},
        )
        client = AsyncMock()
        client.post.return_value = response
        client.__aenter__.return_value = client
        client.__aexit__.return_value = False

        with (
            patch.object(fallback_alerts.httpx, "AsyncClient", return_value=client),
            patch.object(fallback_alerts, "create_alert_event", AsyncMock(return_value="alert_456")),
            patch.object(fallback_alerts, "complete_alert_event", AsyncMock()) as complete_event,
        ):
            delivered = await fallback_alerts._send_upstream_failure_burst_alert(notification)

        self.assertFalse(delivered)
        completion = complete_event.call_args.kwargs
        self.assertEqual(completion["delivery_status"], "failed")
        self.assertEqual(completion["response_status"], 502)
        self.assertNotIn("cloudflare", completion["error_summary"].lower())
        self.assertNotIn("sensitive", completion["error_summary"].lower())

    async def test_http_200_without_dingtalk_result_is_not_marked_sent(self) -> None:
        notification = fallback_alerts.UpstreamFailureBurstNotification(
            alert=self._alert("ccreq_invalid_dingtalk"),
            category="availability",
            count=5,
            window_seconds=60,
        )
        response = SimpleNamespace(status_code=200, json=lambda: {"unexpected": "html edge response"})
        client = AsyncMock()
        client.post.return_value = response
        client.__aenter__.return_value = client
        client.__aexit__.return_value = False

        with (
            patch.object(fallback_alerts.httpx, "AsyncClient", return_value=client),
            patch.object(fallback_alerts, "create_alert_event", AsyncMock(return_value="alert_invalid")),
            patch.object(fallback_alerts, "complete_alert_event", AsyncMock()) as complete_event,
        ):
            delivered = await fallback_alerts._send_upstream_failure_burst_alert(notification)

        self.assertFalse(delivered)
        complete_event.assert_awaited_once_with(
            "alert_invalid",
            delivery_status="failed",
            response_status=200,
            error_summary="DingTalk invalid response",
        )

    async def test_delivery_exception_log_does_not_render_webhook_url(self) -> None:
        notification = fallback_alerts.UpstreamFailureBurstNotification(
            alert=self._alert("ccreq_secret_log"),
            category="availability",
            count=5,
            window_seconds=60,
        )
        secret_url = "https://oapi.dingtalk.com/robot/send?access_token=must-not-log"
        settings.fallback_alert_webhook_url = secret_url
        client = AsyncMock()
        client.post.side_effect = httpx.ConnectError(
            "connection failed",
            request=httpx.Request("POST", secret_url),
        )
        client.__aenter__.return_value = client
        client.__aexit__.return_value = False

        with (
            patch.object(fallback_alerts.httpx, "AsyncClient", return_value=client),
            patch.object(fallback_alerts, "create_alert_event", AsyncMock(return_value="alert_exception")),
            patch.object(fallback_alerts, "complete_alert_event", AsyncMock()),
            patch.object(fallback_alerts.logger, "warning") as warning,
        ):
            delivered = await fallback_alerts._send_upstream_failure_burst_alert(notification)

        self.assertFalse(delivered)
        self.assertNotIn("must-not-log", " ".join(str(value) for value in warning.call_args.args))
        self.assertFalse(warning.call_args.kwargs.get("exc_info", False))

    async def test_httpx_request_logs_do_not_render_webhook_token_for_any_sender(self) -> None:
        secret_token = "must-not-appear-in-httpx-logs"
        webhook_url = (
            "https://oapi.dingtalk.com/robot/send?access_token=" + secret_token
        )
        fallback_alerts.set_runtime_alert_settings(
            {"fallback_alert_webhook_url": webhook_url}
        )
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"errcode": 0})
        )
        real_async_client = httpx.AsyncClient

        def real_client_with_mock_transport(**kwargs):
            return real_async_client(transport=transport, **kwargs)

        messages = []
        handler = logging.Handler()
        handler.emit = lambda record: messages.append(record.getMessage())
        httpx_logger = logging.getLogger("httpx")
        original_level = httpx_logger.level
        httpx_logger.addHandler(handler)
        httpx_logger.setLevel(logging.INFO)
        configured_level = httpx_logger.level
        try:
            with (
                patch.object(
                    fallback_alerts.httpx,
                    "AsyncClient",
                    side_effect=real_client_with_mock_transport,
                ),
                patch.object(
                    fallback_alerts,
                    "create_alert_event",
                    AsyncMock(return_value=None),
                ),
            ):
                fallback_result = await fallback_alerts._send_dingtalk_alert(
                    fallback_alerts.FallbackExhaustedAlert(
                        endpoint="messages",
                        model="claude-sonnet-4-6",
                        status_code=503,
                        reason="503",
                        route_reason="channel_fallback:all_failed",
                        route_attempt=2,
                    )
                )
                burst_result = await fallback_alerts._send_upstream_failure_burst_alert(
                    fallback_alerts.UpstreamFailureBurstNotification(
                        alert=self._alert("ccreq_httpx_log"),
                        category="availability",
                        count=5,
                        window_seconds=60,
                    )
                )
                test_result = await fallback_alerts.send_dingtalk_configuration_test()
                httpx_logger.info("unrelated httpx info remains visible")
                level_after_sends = httpx_logger.level
        finally:
            httpx_logger.removeHandler(handler)
            httpx_logger.setLevel(original_level)

        self.assertTrue(fallback_result)
        self.assertTrue(burst_result)
        self.assertTrue(test_result["sent"])
        rendered_messages = "\n".join(messages)
        self.assertEqual(httpx_logger.level, original_level)
        self.assertEqual(configured_level, logging.INFO)
        self.assertEqual(level_after_sends, configured_level)
        self.assertNotIn(secret_token, rendered_messages)
        self.assertIn("access_token=[REDACTED]", rendered_messages)
        self.assertIn("unrelated httpx info remains visible", rendered_messages)
        installed_filters = [
            installed_filter
            for installed_filter in httpx_logger.filters
            if getattr(
                installed_filter,
                "_coincoin_dingtalk_token_filter",
                False,
            )
        ]
        self.assertEqual(len(installed_filters), 1)

    async def test_httpx_request_log_redaction_handles_quotes_without_changing_other_urls(self) -> None:
        cases = (
            (
                "https://oapi.dingtalk.com/robot/send?access_token=prefix'suffix",
                ("prefix", "suffix"),
                "access_token=[REDACTED]",
            ),
            (
                "https://oapi.dingtalk.com/robot/send?note='&access_token=after-note-secret",
                ("after-note-secret",),
                "note='&access_token=[REDACTED]",
            ),
        )
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"errcode": 0})
        )
        real_async_client = httpx.AsyncClient

        def real_client_with_mock_transport(**kwargs):
            return real_async_client(transport=transport, **kwargs)

        messages = []
        handler = logging.Handler()
        handler.emit = lambda record: messages.append(record.getMessage())
        httpx_logger = logging.getLogger("httpx")
        original_level = httpx_logger.level
        httpx_logger.addHandler(handler)
        httpx_logger.setLevel(logging.INFO)
        try:
            with (
                patch.object(
                    fallback_alerts.httpx,
                    "AsyncClient",
                    side_effect=real_client_with_mock_transport,
                ),
                patch.object(
                    fallback_alerts,
                    "create_alert_event",
                    AsyncMock(return_value=None),
                ),
            ):
                for webhook_url, secret_fragments, expected_url in cases:
                    with self.subTest(webhook_url=webhook_url):
                        messages.clear()
                        fallback_alerts.set_runtime_alert_settings(
                            {"fallback_alert_webhook_url": webhook_url}
                        )

                        result = await fallback_alerts.send_dingtalk_configuration_test()

                        self.assertTrue(result["sent"])
                        rendered_messages = "\n".join(messages)
                        for secret_fragment in secret_fragments:
                            self.assertNotIn(secret_fragment, rendered_messages)
                        self.assertIn(expected_url, rendered_messages)

                messages.clear()
                unrelated_url = "https://example.com/path?note='&safe=value"
                async with real_async_client(transport=transport) as client:
                    await client.get(unrelated_url)
                self.assertIn(unrelated_url, "\n".join(messages))
        finally:
            httpx_logger.removeHandler(handler)
            httpx_logger.setLevel(original_level)

    async def test_hanging_audit_insert_cannot_suppress_dingtalk_delivery(self) -> None:
        notification = fallback_alerts.UpstreamFailureBurstNotification(
            alert=self._alert("ccreq_audit_timeout"),
            category="availability",
            count=5,
            window_seconds=60,
        )
        never = asyncio.Event()

        async def hanging_insert(**fields):
            await never.wait()

        response = SimpleNamespace(status_code=200, json=lambda: {"errcode": 0})
        client = AsyncMock()
        client.post.return_value = response
        client.__aenter__.return_value = client
        client.__aexit__.return_value = False

        with (
            patch.object(fallback_alerts, "_ALERT_HISTORY_TIMEOUT_SECONDS", 0.01),
            patch.object(fallback_alerts, "create_alert_event", side_effect=hanging_insert),
            patch.object(fallback_alerts, "complete_alert_event", AsyncMock()),
            patch.object(fallback_alerts.httpx, "AsyncClient", return_value=client),
        ):
            delivered = await asyncio.wait_for(
                fallback_alerts._send_upstream_failure_burst_alert(notification),
                timeout=0.1,
            )

        self.assertTrue(delivered)
        client.post.assert_awaited_once()

    async def test_hanging_audit_completion_is_bounded_after_delivery(self) -> None:
        notification = fallback_alerts.UpstreamFailureBurstNotification(
            alert=self._alert("ccreq_completion_timeout"),
            category="availability",
            count=5,
            window_seconds=60,
        )
        never = asyncio.Event()

        async def hanging_completion(event_id, **fields):
            await never.wait()

        response = SimpleNamespace(status_code=200, json=lambda: {"errcode": 0})
        client = AsyncMock()
        client.post.return_value = response
        client.__aenter__.return_value = client
        client.__aexit__.return_value = False

        with (
            patch.object(fallback_alerts, "_ALERT_HISTORY_TIMEOUT_SECONDS", 0.01),
            patch.object(fallback_alerts, "create_alert_event", AsyncMock(return_value="alert_completion_timeout")),
            patch.object(fallback_alerts, "complete_alert_event", side_effect=hanging_completion),
            patch.object(fallback_alerts.httpx, "AsyncClient", return_value=client),
        ):
            delivered = await asyncio.wait_for(
                fallback_alerts._send_upstream_failure_burst_alert(notification),
                timeout=0.1,
            )

        self.assertTrue(delivered)
        client.post.assert_awaited_once()

    async def test_configuration_test_is_labelled_and_records_delivery_event(self) -> None:
        response = SimpleNamespace(status_code=200, json=lambda: {"errcode": 0})
        client = AsyncMock()
        client.post.return_value = response
        client.__aenter__.return_value = client
        client.__aexit__.return_value = False

        with (
            patch.object(fallback_alerts.httpx, "AsyncClient", return_value=client),
            patch.object(fallback_alerts, "create_alert_event", AsyncMock(return_value="alt_test")) as create_event,
            patch.object(fallback_alerts, "complete_alert_event", AsyncMock()) as complete_event,
        ):
            result = await fallback_alerts.send_dingtalk_configuration_test()

        self.assertEqual(result, {"sent": True, "event_id": "alt_test"})
        content = client.post.call_args.kwargs["json"]["text"]["content"]
        self.assertIn("CoinCoin", content)
        self.assertIn("配置测试", content)
        create_event.assert_awaited_once()
        self.assertEqual(create_event.call_args.kwargs["category"], "configuration_test")
        complete_event.assert_awaited_once_with(
            "alt_test",
            delivery_status="sent",
            response_status=200,
            error_summary="",
        )

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

    async def test_delivery_scheduler_uses_same_pending_task_limit(self) -> None:
        settings.fallback_alert_max_pending_tasks = 1
        blocker = asyncio.create_task(asyncio.Event().wait())
        fallback_alerts._UPSTREAM_FAILURE_TASKS.add(blocker)
        notification = fallback_alerts.UpstreamFailureBurstNotification(
            alert=self._alert("ccreq_delivery_overflow"),
            category="availability",
            count=5,
            window_seconds=60,
        )
        try:
            with patch.object(fallback_alerts, "_send_upstream_failure_burst_alert", AsyncMock()) as send:
                scheduled = fallback_alerts.notify_upstream_failure_burst(notification)

            self.assertFalse(scheduled)
            send.assert_not_called()
        finally:
            fallback_alerts._UPSTREAM_FAILURE_TASKS.discard(blocker)
            blocker.cancel()
            await asyncio.gather(blocker, return_exceptions=True)

    async def test_fallback_exhausted_scheduler_uses_same_pending_task_limit(self) -> None:
        settings.fallback_alert_max_pending_tasks = 1
        blocker = asyncio.create_task(asyncio.Event().wait())
        fallback_alerts._UPSTREAM_FAILURE_TASKS.add(blocker)
        alert = fallback_alerts.FallbackExhaustedAlert(
            endpoint="messages",
            model="claude-sonnet-4-6",
            status_code=503,
            reason="503",
            route_reason="channel_fallback:all_failed",
            route_attempt=2,
        )
        try:
            with patch.object(fallback_alerts, "_send_dingtalk_alert", AsyncMock()) as send:
                scheduled = fallback_alerts.notify_fallback_exhausted(alert)

            self.assertFalse(scheduled)
            send.assert_not_called()
        finally:
            fallback_alerts._UPSTREAM_FAILURE_TASKS.discard(blocker)
            blocker.cancel()
            await asyncio.gather(blocker, return_exceptions=True)

    async def test_single_task_capacity_can_count_and_deliver_threshold_alert(self) -> None:
        settings.fallback_alert_max_pending_tasks = 1
        settings.upstream_failure_alert_threshold = 1
        delivered = asyncio.Event()

        async def delivery(notification):
            delivered.set()
            return True

        with patch.object(fallback_alerts, "_send_upstream_failure_burst_alert", side_effect=delivery) as send:
            scheduled = fallback_alerts.schedule_user_upstream_failure(self._alert("ccreq_one_slot"))
            self.assertTrue(scheduled)
            await asyncio.wait_for(delivered.wait(), timeout=0.1)
            await fallback_alerts.shutdown_fallback_alerts(timeout_seconds=0.1)

        send.assert_awaited_once()

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
