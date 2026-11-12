import gzip
import json
import os
import unittest
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("COINCOIN_DATABASE_URL", "mysql://test@127.0.0.1:3306/test")

from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

import app.anthropic_compat as anthropic_module
from app.config import settings
from app.router import registry
from app.channel_router import channel_router
from app.request_decompression import (
    RequestDecompressionASGIMiddleware,
    decode_body,
    parse_content_encoding,
)


def _echo_app(limit=None):
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request):
        raw = await request.body()
        return {
            "payload": json.loads(raw),
            "content_encoding": request.headers.get("content-encoding"),
            "content_length": request.headers.get("content-length"),
            "raw_len": len(raw),
        }

    if limit is None:
        app.add_middleware(RequestDecompressionASGIMiddleware)
    else:
        app.add_middleware(RequestDecompressionASGIMiddleware, max_decompressed_bytes=limit)
    return app


PAYLOAD = {
    "model": "gpt-6.1-sol",
    "messages": [{"role": "user", "content": "上下文 " + "context " * 2000}],
}
RAW = json.dumps(PAYLOAD, ensure_ascii=False).encode("utf-8")


class RequestDecompressionTests(unittest.IsolatedAsyncioTestCase):
    async def _post(self, body, encoding=None, app=None):
        headers = {"content-type": "application/json"}
        if encoding is not None:
            headers["content-encoding"] = encoding
        async with AsyncClient(transport=ASGITransport(app=app or _echo_app()), base_url="http://test") as client:
            return await client.post("/echo", content=body, headers=headers)

    async def test_gzip_body_is_decoded_and_headers_rewritten(self):
        compressed = gzip.compress(RAW)
        self.assertLess(len(compressed) * 4, len(RAW))
        for encoding in ("gzip", "GZIP", "x-gzip", " gzip "):
            with self.subTest(encoding=encoding):
                response = await self._post(compressed, encoding)
                self.assertEqual(response.status_code, 200, response.text)
                data = response.json()
                self.assertEqual(data["payload"], PAYLOAD)
                self.assertIsNone(data["content_encoding"])
                self.assertEqual(data["content_length"], str(len(RAW)))
                self.assertEqual(data["raw_len"], len(RAW))

    async def test_concatenated_gzip_members(self):
        half = len(RAW) // 2
        body = gzip.compress(RAW[:half]) + gzip.compress(RAW[half:])
        response = await self._post(body, "gzip")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["payload"], PAYLOAD)

    async def test_deflate_zlib_and_raw(self):
        raw_deflate = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        raw_body = raw_deflate.compress(RAW) + raw_deflate.flush()
        for body in (zlib.compress(RAW), raw_body):
            with self.subTest(kind="zlib" if body[:1] == b"\x78" else "raw"):
                response = await self._post(body, "deflate")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["payload"], PAYLOAD)

    async def test_stacked_encodings_are_undone_in_reverse_order(self):
        body = gzip.compress(zlib.compress(RAW))  # deflate applied first, then gzip
        response = await self._post(body, "deflate, gzip")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["payload"], PAYLOAD)

    async def test_plain_and_identity_bodies_pass_through(self):
        for encoding in (None, "identity", ""):
            with self.subTest(encoding=encoding):
                response = await self._post(RAW, encoding)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["payload"], PAYLOAD)

    async def test_corrupt_and_truncated_bodies_are_400(self):
        good = gzip.compress(RAW)
        for name, body in (("garbage", b"not gzip at all"), ("truncated", good[: len(good) // 2]), ("trailing junk", good + b"junk!")):
            with self.subTest(case=name):
                response = await self._post(body, "gzip")
                self.assertEqual(response.status_code, 400, response.text)
                error = response.json()
                self.assertEqual(error["type"], "error")
                self.assertEqual(error["error"]["type"], "invalid_request_error")
                self.assertIn("gzip", error["error"]["message"])

    async def test_unsupported_encoding_is_415(self):
        for encoding in ("br", "zstd", "gzip, br"):
            with self.subTest(encoding=encoding):
                response = await self._post(b"\x00\x01\x02", encoding)
                self.assertEqual(response.status_code, 415, response.text)
                self.assertEqual(response.headers.get("accept-encoding"), "gzip, deflate")
                self.assertIn("Unsupported Content-Encoding", response.json()["error"]["message"])

    async def test_decompression_bomb_is_413(self):
        bomb = gzip.compress(b"\x00" * (5 * 1024 * 1024))
        self.assertLess(len(bomb), 10 * 1024)
        response = await self._post(bomb, "gzip", app=_echo_app(limit=1024 * 1024))
        self.assertEqual(response.status_code, 413, response.text)
        # exactly at the limit is fine
        exact = gzip.compress(b"[" + b"0," * 1000 + b"0]")
        ok = await self._post(exact, "gzip", app=_echo_app(limit=len(b"[" + b"0," * 1000 + b"0]")))
        self.assertEqual(ok.status_code, 200, ok.text)

    async def test_body_split_across_receive_messages(self):
        compressed = gzip.compress(RAW)
        chunks = [compressed[i:i + 997] for i in range(0, len(compressed), 997)]
        messages = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]
        seen = {}

        async def inner(scope, receive, send):
            first = await receive()
            seen["body"] = first["body"]
            seen["more_body"] = first["more_body"]
            seen["headers"] = dict(scope["headers"])
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def receive():
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        sent = []

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": "/x",
                 "headers": [(b"content-encoding", b"gzip"), (b"content-length", str(len(compressed)).encode())]}
        await RequestDecompressionASGIMiddleware(inner)(scope, receive, send)
        self.assertEqual(seen["body"], RAW)
        self.assertFalse(seen["more_body"])
        self.assertNotIn(b"content-encoding", seen["headers"])
        self.assertEqual(seen["headers"][b"content-length"], str(len(RAW)).encode())
        self.assertEqual(sent[0]["status"], 204)

    async def test_client_disconnect_during_upload_does_not_call_app(self):
        inner = AsyncMock()
        messages = [{"type": "http.request", "body": b"\x1f\x8b", "more_body": True}, {"type": "http.disconnect"}]

        async def receive():
            return messages.pop(0)

        await RequestDecompressionASGIMiddleware(inner)(
            {"type": "http", "method": "POST", "path": "/x", "headers": [(b"content-encoding", b"gzip")]}, receive, AsyncMock())
        inner.assert_not_awaited()

    async def test_get_and_non_http_scopes_are_untouched(self):
        inner = AsyncMock()
        mw = RequestDecompressionASGIMiddleware(inner)
        receive, send = AsyncMock(), AsyncMock()
        for scope in ({"type": "http", "method": "GET", "headers": [(b"content-encoding", b"gzip")]}, {"type": "lifespan"}):
            await mw(scope, receive, send)
        self.assertEqual(inner.await_count, 2)
        receive.assert_not_awaited()

    def test_helpers(self):
        self.assertEqual(parse_content_encoding("gzip, identity"), (["gzip"], []))
        self.assertEqual(parse_content_encoding("Gzip, BR"), (["gzip", "br"], ["br"]))
        self.assertEqual(decode_body(gzip.compress(b"abc"), ["gzip"]), b"abc")


class CompressedMessagesEndToEndTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        keys = ("router_enabled", "fixed_model", "upstream_base_url", "upstream_api_key", "primary_auth_style")
        self._originals = {key: getattr(settings, key) for key in keys}
        settings.router_enabled = False
        settings.fixed_model = "gpt-5.5"
        settings.upstream_base_url = "https://upstream.example/v1"
        settings.upstream_api_key = "sk-upstream-test"
        settings.primary_auth_style = "bearer"
        channel_router.clear_snapshot()
        registry.init_from_settings()
        self.app = FastAPI()
        self.app.include_router(anthropic_module.router)
        self.app.add_middleware(RequestDecompressionASGIMiddleware)

    def tearDown(self):
        channel_router.clear_snapshot()
        for key, value in self._originals.items():
            setattr(settings, key, value)
        registry._initialized = False

    async def test_count_tokens_reads_gzip_body_like_plain_json(self):
        payload = {"model": "gpt-5.5", "messages": [{"role": "user", "content": "hello world " * 500}]}
        raw = json.dumps(payload).encode()
        fake_user = SimpleNamespace(id="u_test", status="active")
        results = {}
        with patch.object(anthropic_module, "authenticate_user", AsyncMock(return_value=fake_user)):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as client:
                for name, body, headers in (
                    ("plain", raw, {}),
                    ("gzip", gzip.compress(raw), {"content-encoding": "gzip"}),
                ):
                    response = await client.post(
                        "/v1/messages/count_tokens",
                        content=body,
                        headers={"authorization": "Bearer sk_test", "anthropic-version": "2023-06-01",
                                 "content-type": "application/json", **headers},
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    results[name] = response.json()["input_tokens"]
        self.assertEqual(results["plain"], results["gzip"])
        self.assertEqual(results["gzip"], 1000)


class MainAppWiringTests(unittest.TestCase):
    def test_decompression_is_innermost_inside_telemetry(self):
        from app.main import app
        from app.request_telemetry import RequestTelemetryASGIMiddleware

        classes = [m.cls for m in app.user_middleware]
        self.assertIs(classes[-1], RequestDecompressionASGIMiddleware)
        self.assertIs(classes[-2], RequestTelemetryASGIMiddleware)


if __name__ == "__main__":
    unittest.main()
