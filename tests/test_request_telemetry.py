import asyncio
import json
import unittest

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.request_telemetry import (
    MAX_SCAN_BYTES,
    FirstTokenScanner,
    RequestTelemetryASGIMiddleware,
    note_request_payload,
    request_log_fields,
)
from app.usage_buffer import UsageBuffer


def sse(event, data):
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {json.dumps(data) if not isinstance(data, str) else data}\n\n".encode()


def first_hit(chunks):
    """Index of the chunk that completes the first output event, or None."""
    scanner = FirstTokenScanner()
    for index, chunk in enumerate(chunks):
        if scanner.feed(chunk):
            return index
    return None


class FirstTokenScannerTests(unittest.TestCase):
    def test_responses_preamble_is_skipped_like_sub2api(self):
        for named in (True, False):
            with self.subTest(named_events=named):
                def event(kind, **extra):
                    return sse(kind if named else None, {"type": kind, **extra})
                chunks = [
                    event("response.created", response={"instructions": "x" * 5000}),
                    event("response.in_progress"),
                    event("response.output_item.added", item={"type": "reasoning"}),
                    event("response.output_text.delta", delta="hi"),
                ]
                self.assertEqual(first_hit(chunks), 2)

    def test_anthropic_message_start_and_ping_are_preamble(self):
        chunks = [
            sse("message_start", {"type": "message_start"}),
            sse("ping", {"type": "ping"}),
            sse("content_block_start", {"type": "content_block_start"}),
        ]
        self.assertEqual(first_hit(chunks), 2)

    def test_chat_role_only_chunk_waits_for_content_or_tool_call(self):
        role = {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]}
        usage_only = {"choices": [], "usage": {"prompt_tokens": 3}}
        for delta in ({"content": "Hi"}, {"reasoning_content": "think"}, {"tool_calls": [{"index": 0}]}):
            with self.subTest(delta=delta):
                chunks = [sse(None, role), sse(None, usage_only), sse(None, {"choices": [{"index": 0, "delta": delta}]})]
                self.assertEqual(first_hit(chunks), 2)

    def test_errors_done_and_comments_never_count(self):
        chunks = [
            b": keepalive\n\n",
            sse("error", {"type": "error", "error": {"code": "server_is_overloaded"}}),
            sse(None, {"error": {"message": "upstream"}}),
            sse("response.failed", {"type": "response.failed"}),
            b"data: [DONE]\n\n",
        ]
        self.assertIsNone(first_hit(chunks))

    def test_events_split_across_chunks_and_crlf(self):
        payload = sse("response.created", {"type": "response.created"}) + sse(
            "response.output_text.delta", {"delta": "x"}).replace(b"\n", b"\r\n")
        cut = len(payload) - 7  # split inside the output event's data line
        self.assertEqual(first_hit([payload[:10], payload[10:cut], payload[cut:]]), 2)

    def test_default_message_event_is_classified_by_data(self):
        chunks = [sse("message", {"type": "response.created"}), sse("message", {"type": "response.output_text.delta"})]
        self.assertEqual(first_hit(chunks), 1)

    def test_oversized_preamble_gives_up_instead_of_buffering(self):
        scanner = FirstTokenScanner()
        self.assertFalse(scanner.feed(b"data: " + b"x" * MAX_SCAN_BYTES))
        self.assertTrue(scanner.done)
        self.assertFalse(scanner.feed(sse(None, {"choices": [{"delta": {"content": "late"}}]})))


class RequestPayloadTests(unittest.TestCase):
    def test_outside_a_request_nothing_is_recorded(self):
        note_request_payload({"stream": True})
        self.assertEqual(request_log_fields(), {})


def build_app(buffer: UsageBuffer) -> FastAPI:
    app = FastAPI()

    async def log(duration_ms: int):
        await buffer.add(user_id="u1", input_tokens=10, output_tokens=5, requests=1, duration_ms=duration_ms)

    @app.post("/stream")
    async def stream(request: Request):
        note_request_payload(await request.json())

        async def body():
            yield sse("response.created", {"type": "response.created"})
            await asyncio.sleep(0.15)
            yield sse("response.output_text.delta", {"type": "response.output_text.delta", "delta": "hi"})
            await log(10_000)

        return StreamingResponse(body(), media_type="text/event-stream")

    @app.post("/json")
    async def plain(request: Request):
        note_request_payload(await request.json())
        await log(50)
        return JSONResponse({"ok": True})

    app.add_middleware(RequestTelemetryASGIMiddleware)
    return app


class RequestTelemetryMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.buffer = UsageBuffer()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(self.buffer)), base_url="http://t.test")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_stream_log_gets_first_token_after_preamble_and_client_metadata(self):
        response = await self.client.post(
            "/stream",
            json={"stream": True, "reasoning": {"effort": "xhigh"}},
            headers={"user-agent": "codex_cli_rs/0.80.0 (Windows 11)", "x-forwarded-for": "203.0.113.9, 10.0.0.1"},
        )
        self.assertEqual(response.status_code, 200)
        log = self.buffer._request_logs[0]
        self.assertGreaterEqual(log["first_token_ms"], 150)
        self.assertLess(log["first_token_ms"], 5_000)
        self.assertEqual((log["is_stream"], log["reasoning_effort"]), (True, "xhigh"))
        self.assertEqual((log["user_agent"], log["client_ip"]), ("codex_cli_rs/0.80.0 (Windows 11)", "203.0.113.9"))

    async def test_non_stream_log_has_no_first_token(self):
        for body, effort in (({"reasoning_effort": "high"}, "high"), ({"output_config": {"effort": "max"}}, "max")):
            with self.subTest(body=body):
                self.buffer._request_logs.clear()
                response = await self.client.post("/json", json=body, headers={"x-real-ip": "198.51.100.7"})
                self.assertEqual(response.status_code, 200)
                log = self.buffer._request_logs[0]
                self.assertIsNone(log["first_token_ms"])
                self.assertEqual((log["is_stream"], log["reasoning_effort"], log["client_ip"]), (False, effort, "198.51.100.7"))

    async def test_first_token_never_exceeds_logged_duration(self):
        buffer = UsageBuffer()
        app = FastAPI()

        @app.get("/s")
        async def stream():
            async def body():
                await asyncio.sleep(0.05)
                yield sse(None, {"choices": [{"delta": {"content": "x"}}]})
                await buffer.add(user_id="u1", output_tokens=1, requests=1, duration_ms=10)
            return StreamingResponse(body(), media_type="text/event-stream")

        app.add_middleware(RequestTelemetryASGIMiddleware)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t.test") as client:
            await client.get("/s")
        self.assertEqual(buffer._request_logs[0]["first_token_ms"], 10)


if __name__ == "__main__":
    unittest.main()
