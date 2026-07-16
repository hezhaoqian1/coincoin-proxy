import asyncio
import json
import time
import unittest

from app.image_keepalive import ImageJSONKeepaliveASGIMiddleware


def _scope(path: str, method: str = "POST") -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 443),
    }


async def _unused_receive() -> dict:
    await asyncio.sleep(3600)
    return {"type": "http.disconnect"}


def _headers(message: dict) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in message.get("headers", [])
    }


class ImageJSONKeepaliveMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, app, *, path="/v1/images/generations", interval=0.01):
        messages = []

        async def send(message: dict) -> None:
            messages.append((time.monotonic(), dict(message)))

        middleware = ImageJSONKeepaliveASGIMiddleware(app, interval_seconds=interval)
        await middleware(_scope(path), _unused_receive, send)
        return messages

    async def test_disabled_mode_preserves_original_response(self) -> None:
        async def app(scope, receive, send):
            await asyncio.sleep(0.02)
            await send({"type": "http.response.start", "status": 502, "headers": [(b"x-test", b"fast-error")]})
            await send({"type": "http.response.body", "body": b'{"error":{"code":"upstream"}}', "more_body": False})

        messages = await self._run(app, interval=0)

        self.assertEqual(messages[0][1]["status"], 502)
        self.assertEqual(_headers(messages[0][1])["x-test"], "fast-error")
        self.assertEqual(messages[1][1]["body"], b'{"error":{"code":"upstream"}}')

    async def test_fast_response_preserves_status_headers_and_body(self) -> None:
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 201, "headers": [(b"x-request-id", b"req-fast")]})
            await send({"type": "http.response.body", "body": b'{"data":[{"b64_json":"ok"}]}', "more_body": False})

        messages = await self._run(app)

        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0][1]["status"], 201)
        self.assertEqual(_headers(messages[0][1])["x-request-id"], "req-fast")
        self.assertEqual(json.loads(messages[1][1]["body"]), {"data": [{"b64_json": "ok"}]})

    async def test_fast_error_preserves_original_status_headers_and_body(self) -> None:
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 502, "headers": [(b"x-request-id", b"req-error")]})
            await send({"type": "http.response.body", "body": b'{"error":{"code":"upstream_error"}}', "more_body": False})

        messages = await self._run(app)

        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0][1]["status"], 502)
        self.assertEqual(_headers(messages[0][1])["x-request-id"], "req-error")
        self.assertEqual(json.loads(messages[1][1]["body"]), {"error": {"code": "upstream_error"}})

    async def test_slow_success_emits_json_safe_whitespace_heartbeats(self) -> None:
        async def app(scope, receive, send):
            await asyncio.sleep(0.035)
            await send({"type": "http.response.start", "status": 200, "headers": [(b"x-request-id", b"req-slow")]})
            await send({"type": "http.response.body", "body": b'{"data":[{"b64_json":"slow"}]}', "more_body": False})

        messages = await self._run(app)

        start = messages[0][1]
        self.assertEqual(start["status"], 200)
        self.assertEqual(sum(message.get("type") == "http.response.start" for _, message in messages), 1)
        headers = _headers(start)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertEqual(headers["cache-control"], "no-cache, no-store, no-transform")
        self.assertEqual(headers["x-accel-buffering"], "no")
        self.assertEqual(headers["x-coincoin-image-keepalive"], "whitespace")

        body_messages = [message for _, message in messages[1:]]
        heartbeat_messages = [message for message in body_messages[:-1] if message.get("body") == b" \n"]
        self.assertGreaterEqual(len(heartbeat_messages), 1)
        self.assertTrue(all(message.get("more_body") for message in heartbeat_messages))

        body = b"".join(message.get("body", b"") for message in body_messages)
        self.assertTrue(body.startswith(b" \n"))
        self.assertEqual(json.loads(body), {"data": [{"b64_json": "slow"}]})
        self.assertFalse(body_messages[-1].get("more_body", False))

    async def test_slow_error_keeps_json_body_after_status_is_committed(self) -> None:
        async def app(scope, receive, send):
            await asyncio.sleep(0.025)
            await send({"type": "http.response.start", "status": 524, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"error":{"code":"upstream_timeout"}}', "more_body": False})

        messages = await self._run(app)

        self.assertEqual(messages[0][1]["status"], 200)
        body = b"".join(message.get("body", b"") for _, message in messages[1:])
        self.assertEqual(json.loads(body), {"error": {"code": "upstream_timeout"}})

    async def test_non_image_path_bypasses_keepalive(self) -> None:
        async def app(scope, receive, send):
            await asyncio.sleep(0.025)
            await send({"type": "http.response.start", "status": 503, "headers": []})
            await send({"type": "http.response.body", "body": b'{"error":"text route"}', "more_body": False})

        messages = await self._run(app, path="/v1/chat/completions")

        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0][1]["status"], 503)
        self.assertEqual(messages[1][1]["body"], b'{"error":"text route"}')

    async def test_all_public_sync_image_paths_are_covered(self) -> None:
        async def app(scope, receive, send):
            await asyncio.sleep(0.025)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b'{"data":[]}', "more_body": False})

        for path in (
            "/v1/images/generations",
            "/v1/images/edits",
            "/openai/v1/images/generations",
            "/openai/v1/images/edits",
        ):
            with self.subTest(path=path):
                messages = await self._run(app, path=path)
                self.assertEqual(_headers(messages[0][1])["x-coincoin-image-keepalive"], "whitespace")
                body = b"".join(message.get("body", b"") for _, message in messages[1:])
                self.assertEqual(json.loads(body), {"data": []})

    async def test_non_post_image_request_bypasses_keepalive(self) -> None:
        async def app(scope, receive, send):
            await asyncio.sleep(0.025)
            await send({"type": "http.response.start", "status": 405, "headers": [(b"allow", b"POST")]})
            await send({"type": "http.response.body", "body": b'{"detail":"Method Not Allowed"}', "more_body": False})

        messages = []

        async def send(message: dict) -> None:
            messages.append(dict(message))

        middleware = ImageJSONKeepaliveASGIMiddleware(app, interval_seconds=0.01)
        await middleware(_scope("/v1/images/generations", method="GET"), _unused_receive, send)

        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["status"], 405)
        self.assertNotIn("x-coincoin-image-keepalive", _headers(messages[0]))

    async def test_main_middleware_order_keeps_cors_outside_and_quota_inside(self) -> None:
        from app.main import app

        self.assertEqual(
            [item.cls.__name__ for item in app.user_middleware[:3]],
            ["CORSMiddleware", "ImageJSONKeepaliveASGIMiddleware", "QuotaReservationASGIMiddleware"],
        )

    async def test_late_unhandled_exception_becomes_json_error(self) -> None:
        async def app(scope, receive, send):
            await asyncio.sleep(0.025)
            raise RuntimeError("boom")

        with self.assertLogs("coincoin.image_keepalive", level="ERROR"):
            messages = await self._run(app)

        self.assertEqual(messages[0][1]["status"], 200)
        body = b"".join(message.get("body", b"") for _, message in messages[1:])
        payload = json.loads(body)
        self.assertEqual(payload["error"]["code"], "image_keepalive_internal_error")
        self.assertNotIn("boom", payload["error"]["message"])

    async def test_send_failure_cancels_slow_downstream(self) -> None:
        cancelled = asyncio.Event()

        async def app(scope, receive, send):
            try:
                await asyncio.sleep(3600)
            finally:
                cancelled.set()

        async def send(message: dict) -> None:
            if message.get("type") == "http.response.body":
                raise ConnectionError("client disconnected")

        middleware = ImageJSONKeepaliveASGIMiddleware(app, interval_seconds=0.01)
        with self.assertRaises(ConnectionError):
            await middleware(_scope("/v1/images/edits"), _unused_receive, send)

        await asyncio.wait_for(cancelled.wait(), timeout=0.2)


if __name__ == "__main__":
    unittest.main()
