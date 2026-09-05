import unittest
from unittest.mock import Mock, patch

from app.admin_timing import AdminTimingASGIMiddleware


class AdminTimingMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, path: str, *, logger=None):
        messages = []

        async def downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        middleware = AdminTimingASGIMiddleware(
            downstream,
            slow_threshold_ms=1000,
            logger=logger,
        )
        scope = {"type": "http", "method": "GET", "path": path}
        await middleware(scope, receive, send)
        return messages

    async def test_admin_response_exposes_server_timing_headers(self) -> None:
        with patch("app.admin_timing.time.perf_counter", side_effect=[10.0, 10.125]):
            messages = await self._call("/admin/users")

        start = messages[0]
        headers = dict(start["headers"])
        self.assertEqual(headers[b"x-process-time-ms"], b"125.00")
        self.assertEqual(headers[b"server-timing"], b"app;dur=125.00")

    async def test_slow_admin_response_logs_only_method_path_status_and_duration(self) -> None:
        logger = Mock()
        with patch("app.admin_timing.time.perf_counter", side_effect=[10.0, 11.5]):
            await self._call("/admin/analytics/operating-dashboard", logger=logger)

        logger.warning.assert_called_once_with(
            "slow admin request method=%s path=%s status=%s duration_ms=%.2f",
            "GET",
            "/admin/analytics/operating-dashboard",
            200,
            1500.0,
        )

    async def test_non_admin_response_is_untouched(self) -> None:
        logger = Mock()
        messages = await self._call("/v1/models", logger=logger)

        self.assertEqual(messages[0]["headers"], [])
        logger.warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
