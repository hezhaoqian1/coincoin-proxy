import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.config import settings
from app.main import app
from app.router import registry
import app.proxy as proxy_module
import app.openai_compat as openai_module


LEGACY_PUBLIC_TEXT_MODELS = [
    "gpt-5.4",
    "gpt-5",
    "gpt-5.1",
    "gpt-5.1-codex",
    "gpt-5.1-codex-mini",
    "gpt-5.1-codex-max",
    "gpt-5.2",
    "gpt-5.2-codex",
    "gpt-5.3-codex",
    "gpt-5.4-mini",
    "gpt-5-codex",
    "gpt-5-codex-mini",
]


def _legacy_text_model(model_id: str) -> dict:
    return {
        "id": model_id,
        "owned_by": "openai",
        "provider_name": "OpenAI",
        "capabilities": ["chat/completions", "responses"],
        "routing_mode": "legacy_auto",
        "delivery_lane": "legacy",
    }


class _FakeUpstreamResponse:
    def __init__(
        self,
        payload,
        status_code: int = 200,
        content_type: str = "application/json",
        headers: dict | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = {"content-type": content_type, **(headers or {})}
        self._closed = False

    def json(self):
        return self._payload

    @property
    def text(self) -> str:
        if isinstance(self._payload, (dict, list)):
            return json.dumps(self._payload, ensure_ascii=False)
        if isinstance(self._payload, bytes):
            return self._payload.decode("utf-8", errors="replace")
        return str(self._payload)

    async def aread(self) -> bytes:
        return self.text.encode("utf-8")

    async def aiter_bytes(self):
        body = self.text.encode("utf-8")
        midpoint = max(1, len(body) // 2)
        yield body[:midpoint]
        yield body[midpoint:]

    async def aclose(self) -> None:
        self._closed = True


class _FakeEventStreamResponse:
    def __init__(self, lines, status_code: int = 200, headers: dict | None = None) -> None:
        self._lines = list(lines)
        self.status_code = status_code
        self.headers = {"content-type": "text/event-stream", **(headers or {})}
        self._closed = False

    @property
    def text(self) -> str:
        return "\n".join(self._lines)

    async def aread(self) -> bytes:
        return self.text.encode("utf-8")

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aclose(self) -> None:
        self._closed = True


class _RecordingClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected upstream call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _RecordingStreamClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def build_request(self, method, url, **kwargs):
        request = {"method": method, "url": url, **kwargs}
        self.calls.append(request)
        return request

    async def send(self, request, stream=False):
        if not stream:
            raise AssertionError("expected stream=True")
        if not self.responses:
            raise AssertionError("unexpected upstream stream call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class OpenAICompatDefaultsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._originals = {
            "fixed_model": settings.fixed_model,
            "embedding_model": settings.embedding_model,
            "embedding_upstream_url": settings.embedding_upstream_url,
            "embedding_api_key": settings.embedding_api_key,
            "embedding_auth_style": settings.embedding_auth_style,
            "embedding_price_input": settings.embedding_price_input,
            "router_enabled": settings.router_enabled,
            "upstream_base_url": settings.upstream_base_url,
            "upstream_api_key": settings.upstream_api_key,
            "price_input_per_million": settings.price_input_per_million,
            "price_output_per_million": settings.price_output_per_million,
            "primary_auth_style": settings.primary_auth_style,
            "primary_strip_unsupported": settings.primary_strip_unsupported,
            "cheap_model": settings.cheap_model,
            "cheap_upstream_url": settings.cheap_upstream_url,
            "cheap_api_key": settings.cheap_api_key,
            "cheap_price_input": settings.cheap_price_input,
            "cheap_price_output": settings.cheap_price_output,
            "fallback_model": settings.fallback_model,
            "fallback_upstream_url": settings.fallback_upstream_url,
            "fallback_api_key": settings.fallback_api_key,
            "fallback_price_input": settings.fallback_price_input,
            "fallback_price_output": settings.fallback_price_output,
            "fallback_auth_style": settings.fallback_auth_style,
            "gateway_base_url": settings.gateway_base_url,
            "gateway_api_key": settings.gateway_api_key,
            "gateway_auth_style": settings.gateway_auth_style,
            "image_edit_sync_gateway_timeout_seconds": settings.image_edit_sync_gateway_timeout_seconds,
            "vertex_api_key": settings.vertex_api_key,
            "vertex_gemini_api_base": settings.vertex_gemini_api_base,
            "model_catalog_json": settings.model_catalog_json,
        }

        settings.fixed_model = "gpt-5.4"
        settings.embedding_model = "text-embedding-3-small"
        settings.embedding_upstream_url = ""
        settings.embedding_api_key = ""
        settings.embedding_auth_style = ""
        settings.embedding_price_input = 99
        settings.router_enabled = True
        settings.upstream_base_url = "https://legacy.example/v1"
        settings.upstream_api_key = "legacy-key"
        settings.price_input_per_million = 99
        settings.price_output_per_million = 699
        settings.primary_auth_style = "azure"
        settings.primary_strip_unsupported = False
        settings.cheap_model = "gpt-4o-mini"
        settings.cheap_upstream_url = "https://legacy.example/v1"
        settings.cheap_api_key = "legacy-key"
        settings.cheap_price_input = 15
        settings.cheap_price_output = 60
        settings.fallback_model = "gpt-5.4"
        settings.fallback_upstream_url = "https://fallback.example/v1"
        settings.fallback_api_key = "fallback-key"
        settings.fallback_price_input = 99
        settings.fallback_price_output = 699
        settings.fallback_auth_style = "azure"
        settings.gateway_base_url = ""
        settings.gateway_api_key = ""
        settings.gateway_auth_style = "bearer"
        settings.image_edit_sync_gateway_timeout_seconds = 60
        settings.vertex_api_key = ""
        settings.vertex_gemini_api_base = "https://aiplatform.googleapis.com/v1/publishers/google"
        settings.model_catalog_json = json.dumps(
            {
                "default_text_model": "gpt-5.4",
                "default_embedding_model": "text-embedding-3-small",
                "default_image_model": "gemini-image",
                "models": [
                    *[_legacy_text_model(model_id) for model_id in LEGACY_PUBLIC_TEXT_MODELS],
                    {
                        "id": "text-embedding-3-small",
                        "owned_by": "openai",
                        "provider_name": "OpenAI",
                        "provider_model": "text-embedding-3-small",
                        "capabilities": ["embeddings"],
                        "routing_mode": "direct",
                        "delivery_lane": "upstream_direct",
                        "upstream_model": "text-embedding-3-small",
                        "upstream_url": "https://fallback.example/v1",
                        "api_key": "fallback-key",
                        "auth_style": "azure",
                        "price_input_per_million": 99,
                        "price_output_per_million": 0,
                        "billable_sku": "azure-text-embedding-3-small",
                    },
                    {
                        "id": "gemini-fast",
                        "owned_by": "google",
                        "provider_name": "Google",
                        "provider_model": "gemini-2.5-flash",
                        "capabilities": ["chat/completions", "responses"],
                        "routing_mode": "direct",
                        "delivery_lane": "gateway",
                        "upstream_model": "gemini-fast",
                        "upstream_url": "https://gateway.example/v1",
                        "api_key": "gateway-key",
                        "auth_style": "bearer",
                    },
                    {
                        "id": "gemini-image",
                        "owned_by": "google",
                        "provider_name": "Google",
                        "provider_model": "gemini-3.1-flash-image-preview",
                        "capabilities": ["images/generations", "images/edits"],
                        "routing_mode": "direct",
                        "delivery_lane": "gateway",
                        "upstream_model": "vertex-gemini-3.1-flash-image-preview",
                        "upstream_url": "https://gateway.example/v1",
                        "api_key": "gateway-key",
                        "auth_style": "bearer",
                    },
                ],
            }
        )
        registry._initialized = False
        registry.init_from_settings()
        self.fake_user = SimpleNamespace(id="u_test")

        async def fake_get_db():
            yield None

        app.dependency_overrides[proxy_module.get_db] = fake_get_db
        app.dependency_overrides[openai_module.get_db] = fake_get_db

    def _set_model_delivery_lane(self, model_id: str, delivery_lane: str) -> None:
        catalog = json.loads(settings.model_catalog_json)
        for model in catalog.get("models") or []:
            if model.get("id") == model_id:
                model["delivery_lane"] = delivery_lane
                break
        settings.model_catalog_json = json.dumps(catalog)
        registry._initialized = False
        registry.init_from_settings()

    def tearDown(self) -> None:
        for key, value in self._originals.items():
            setattr(settings, key, value)
        registry._initialized = False
        app.dependency_overrides.pop(proxy_module.get_db, None)
        app.dependency_overrides.pop(openai_module.get_db, None)

    async def test_chat_without_model_keeps_legacy_public_alias(self) -> None:
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "resp_legacy",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": "OK"}],
                            }
                        ],
                        "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
                    }
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(openai_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                openai_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ):
                response = await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={"messages": [{"role": "user", "content": "Reply with only: OK"}]},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["model"], "gpt-5.4")
        self.assertEqual(payload["choices"][0]["message"]["content"], "OK")
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(upstream_client.calls[0]["url"], "https://legacy.example/v1/responses")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "gpt-4o-mini")
        self.assertEqual(upstream_client.calls[0]["headers"]["api-key"], "legacy-key")

    async def test_chat_empty_nonstream_json_collapses_stream_output(self) -> None:
        settings.router_enabled = False
        registry._initialized = False
        registry.init_from_settings()
        upstream_client = _RecordingStreamClient(
            [
                _FakeEventStreamResponse(
                    [
                        'data: {"type":"response.created","response":{"id":"resp_empty_chat","status":"in_progress","model":"gpt-4o-mini","output":[]}}',
                        'data: {"type":"response.output_text.delta","delta":"OK"}',
                        'data: {"type":"response.completed","response":{"id":"resp_empty_chat","status":"completed","model":"gpt-4o-mini","output":[],"usage":{"input_tokens":3,"output_tokens":1,"total_tokens":4}}}',
                        "data: [DONE]",
                    ]
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(openai_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                openai_module,
                "get_stream_client",
                AsyncMock(return_value=upstream_client),
            ), patch.object(openai_module.usage_buffer, "add", AsyncMock()) as add_usage:
                response = await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={"model": "gpt-5.4", "messages": [{"role": "user", "content": "Reply with only: OK"}]},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["choices"][0]["message"]["content"], "OK")
        self.assertEqual(upstream_client.calls[0]["url"], "https://legacy.example/v1/responses")
        self.assertTrue(upstream_client.calls[0]["json"]["stream"])
        add_usage.assert_awaited_once()

    async def test_responses_without_model_keeps_legacy_public_alias(self) -> None:
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "resp_legacy",
                        "object": "chat.completion",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": "OK"}],
                            }
                        ],
                        "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
                    }
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                proxy_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ):
                response = await client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={"input": "Reply with only: OK"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["model"], "gpt-5.4")
        self.assertEqual(payload["output"][0]["content"][0]["text"], "OK")
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(upstream_client.calls[0]["url"], "https://legacy.example/v1/responses")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "gpt-4o-mini")
        self.assertEqual(upstream_client.calls[0]["headers"]["api-key"], "legacy-key")

    async def test_responses_explicit_legacy_alias_keeps_public_model_name(self) -> None:
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "resp_legacy_alias",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": "OK"}],
                            }
                        ],
                        "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
                    }
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                proxy_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ):
                response = await client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={"model": "gpt-5.2-codex", "input": "Reply with only: OK"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["model"], "gpt-5.2-codex")

    async def test_responses_explicit_gpt_5_4_mini_alias_keeps_public_model_name(self) -> None:
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "resp_legacy_mini_alias",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": "OK"}],
                            }
                        ],
                        "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
                    }
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                proxy_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ):
                response = await client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={"model": "gpt-5.4-mini", "input": "Reply with only: OK"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["model"], "gpt-5.4-mini")

    async def test_responses_legacy_lane_drops_context_management(self) -> None:
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "resp_context_strip",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": "OK"}],
                            }
                        ],
                        "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
                    }
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                proxy_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ):
                response = await client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={
                        "model": "gpt-5.2-codex",
                        "input": "Reply with only: OK",
                        "context_management": {"type": "auto"},
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("context_management", upstream_client.calls[0]["json"])

    async def test_responses_cache_miss_drops_previous_response_id(self) -> None:
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "resp_cache_miss",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": "OK"}],
                            }
                        ],
                        "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
                    }
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                proxy_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ), patch.object(proxy_module._conv_cache, "get", return_value=None):
                response = await client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={
                        "model": "gpt-5.2-codex",
                        "input": "Reply with only: OK",
                        "previous_response_id": "resp_missing",
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("previous_response_id", upstream_client.calls[0]["json"])

    async def test_responses_logs_upstream_request_id(self) -> None:
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "resp_reqid",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": "OK"}],
                            }
                        ],
                        "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
                    },
                    headers={"x-request-id": "req_123"},
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                proxy_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ), patch.object(proxy_module.usage_buffer, "add", AsyncMock()) as add_usage:
                response = await client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={"input": "Reply with only: OK"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(add_usage.await_args.kwargs["upstream_request_id"], "req_123")

    async def test_responses_empty_nonstream_json_collapses_stream_output(self) -> None:
        settings.router_enabled = False
        registry._initialized = False
        registry.init_from_settings()
        upstream_client = _RecordingStreamClient(
            [
                _FakeEventStreamResponse(
                    [
                        'data: {"type":"response.created","response":{"id":"resp_empty_responses","status":"in_progress","model":"gpt-4o-mini","output":[]}}',
                        'data: {"type":"response.output_text.delta","delta":"OK"}',
                        'data: {"type":"response.completed","response":{"id":"resp_empty_responses","status":"completed","model":"gpt-4o-mini","output":[],"usage":{"input_tokens":3,"output_tokens":1,"total_tokens":4}}}',
                        "data: [DONE]",
                    ]
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                proxy_module,
                "get_stream_client",
                AsyncMock(return_value=upstream_client),
            ), patch.object(proxy_module.usage_buffer, "add", AsyncMock()) as add_usage:
                response = await client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={"model": "gpt-5.4", "input": "Reply with only: OK"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["output"][0]["content"][0]["text"], "OK")
        self.assertEqual(upstream_client.calls[0]["url"], "https://legacy.example/v1/responses")
        self.assertTrue(upstream_client.calls[0]["json"]["stream"])
        add_usage.assert_awaited_once()

    async def test_responses_gpt_5_4_with_tools_collapses_stream_tool_calls(self) -> None:
        settings.router_enabled = False
        registry._initialized = False
        registry.init_from_settings()
        upstream_client = _RecordingStreamClient(
            [
                _FakeEventStreamResponse(
                    [
                        'data: {"type":"response.created","response":{"id":"resp_tool_json","status":"in_progress","model":"gpt-5.4","output":[]}}',
                        'data: {"type":"response.output_item.added","item":{"type":"function_call","id":"call_123","name":"read_file"}}',
                        'data: {"type":"response.function_call_arguments.delta","delta":"{\\"path\\":\\"foo.txt\\"}"}',
                        'data: {"type":"response.function_call_arguments.done"}',
                        'data: {"type":"response.completed","response":{"id":"resp_tool_json","status":"completed","model":"gpt-5.4","output":[],"usage":{"input_tokens":3,"output_tokens":1,"total_tokens":4}}}',
                        "data: [DONE]",
                    ]
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                proxy_module,
                "get_stream_client",
                AsyncMock(return_value=upstream_client),
            ), patch.object(proxy_module.usage_buffer, "add", AsyncMock()) as add_usage:
                response = await client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={
                        "model": "gpt-5.4",
                        "input": [{"role": "user", "content": "Read foo.txt"}],
                        "tools": [
                            {
                                "type": "function",
                                "name": "read_file",
                                "description": "Read file contents",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"path": {"type": "string"}},
                                    "required": ["path"],
                                },
                            }
                        ],
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["output"][0]["type"], "function_call")
        self.assertEqual(payload["output"][0]["name"], "read_file")
        self.assertEqual(payload["output"][0]["arguments"], "{\"path\":\"foo.txt\"}")
        self.assertEqual(upstream_client.calls[0]["url"], "https://legacy.example/v1/responses")
        self.assertTrue(upstream_client.calls[0]["json"]["stream"])
        self.assertEqual(upstream_client.calls[0]["json"]["tools"][0]["name"], "read_file")
        add_usage.assert_awaited_once()

    async def test_chat_nonstream_gpt_5_4_with_tools_collapses_stream_tool_calls(self) -> None:
        settings.router_enabled = False
        registry._initialized = False
        registry.init_from_settings()
        upstream_client = _RecordingStreamClient(
            [
                _FakeEventStreamResponse(
                    [
                        'data: {"type":"response.created","response":{"id":"resp_tool_chat","status":"in_progress","model":"gpt-5.4","output":[]}}',
                        'data: {"type":"response.output_item.added","item":{"type":"function_call","id":"call_123","name":"read_file"}}',
                        'data: {"type":"response.function_call_arguments.delta","delta":"{\\"path\\":\\"foo.txt\\"}"}',
                        'data: {"type":"response.function_call_arguments.done"}',
                        'data: {"type":"response.completed","response":{"id":"resp_tool_chat","status":"completed","model":"gpt-5.4","output":[],"usage":{"input_tokens":3,"output_tokens":1,"total_tokens":4}}}',
                        "data: [DONE]",
                    ]
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(openai_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                openai_module,
                "get_stream_client",
                AsyncMock(return_value=upstream_client),
            ), patch.object(openai_module.usage_buffer, "add", AsyncMock()) as add_usage:
                response = await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={
                        "model": "gpt-5.4",
                        "messages": [{"role": "user", "content": "Read foo.txt"}],
                        "tools": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "description": "Read file contents",
                                    "parameters": {
                                        "type": "object",
                                        "properties": {"path": {"type": "string"}},
                                        "required": ["path"],
                                    },
                                },
                            }
                        ],
                        "tool_choice": "required",
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        tool_calls = payload["choices"][0]["message"]["tool_calls"]
        self.assertEqual(tool_calls[0]["function"]["name"], "read_file")
        self.assertEqual(tool_calls[0]["function"]["arguments"], "{\"path\":\"foo.txt\"}")
        self.assertTrue(upstream_client.calls[0]["json"]["stream"])
        add_usage.assert_awaited_once()

    async def test_chat_stream_gpt_5_4_with_tools_preserves_tool_call_sse(self) -> None:
        settings.router_enabled = False
        registry._initialized = False
        registry.init_from_settings()
        upstream_client = _RecordingStreamClient(
            [
                _FakeEventStreamResponse(
                    [
                        'data: {"type":"response.created","response":{"id":"resp_tool_stream","status":"in_progress","model":"gpt-5.4","output":[]}}',
                        'data: {"type":"response.output_item.added","item":{"type":"function_call","id":"call_123","name":"read_file"}}',
                        'data: {"type":"response.function_call_arguments.delta","delta":"{\\"path\\":\\"foo.txt\\"}"}',
                        'data: {"type":"response.function_call_arguments.done"}',
                        'data: {"type":"response.completed","response":{"id":"resp_tool_stream","status":"completed","model":"gpt-5.4","output":[{"type":"function_call","id":"call_123","name":"read_file","arguments":"{\\"path\\":\\"foo.txt\\"}"}],"usage":{"input_tokens":3,"output_tokens":1,"total_tokens":4}}}',
                        "data: [DONE]",
                    ]
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(openai_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                openai_module,
                "get_stream_client",
                AsyncMock(return_value=upstream_client),
            ):
                response = await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={
                        "model": "gpt-5.4",
                        "stream": True,
                        "messages": [{"role": "user", "content": "Read foo.txt"}],
                        "tools": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "description": "Read file contents",
                                    "parameters": {
                                        "type": "object",
                                        "properties": {"path": {"type": "string"}},
                                        "required": ["path"],
                                    },
                                },
                            }
                        ],
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('"tool_calls"', response.text)
        self.assertIn('"name": "read_file"', response.text)
        self.assertIn('\\"path\\":\\"foo.txt\\"', response.text)
        self.assertEqual(upstream_client.calls[0]["url"], "https://legacy.example/v1/responses")
        self.assertTrue(upstream_client.calls[0]["json"]["stream"])
        self.assertEqual(upstream_client.calls[0]["json"]["tools"][0]["name"], "read_file")

    async def test_image_generation_uses_gateway_lane_without_vertex_key(self) -> None:
        upstream_client = _RecordingStreamClient(
            [
                _FakeUpstreamResponse(
                    {
                        "created": 1774449999,
                        "data": [{"b64_json": "from-gateway"}],
                    }
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                proxy_module,
                "get_image_stream_client",
                AsyncMock(return_value=upstream_client),
            ):
                response = await client.post(
                    "/v1/images/generations",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={"prompt": "A blue coin mascot", "n": 1, "size": "1024x1024"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"][0]["b64_json"], "from-gateway")
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(upstream_client.calls[0]["url"], "https://gateway.example/v1/images/generations")

    async def test_image_generation_prefers_gateway_lane_when_gateway_is_configured(self) -> None:
        settings.gateway_base_url = "https://gateway.example"
        settings.gateway_api_key = "gateway-key"

        upstream_client = _RecordingStreamClient(
            [
                _FakeUpstreamResponse(
                    {
                        "created": 1774449999,
                        "data": [{"b64_json": "from-gateway"}],
                    }
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                proxy_module,
                "get_image_stream_client",
                AsyncMock(return_value=upstream_client),
            ):
                response = await client.post(
                    "/v1/images/generations",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={"model": "gemini-image", "prompt": "A blue coin mascot", "n": 1, "size": "1024x1024"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"][0]["b64_json"], "from-gateway")
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(
            upstream_client.calls[0]["url"],
            "https://gateway.example/v1/images/generations",
        )
        self.assertEqual(
            upstream_client.calls[0]["json"]["model"],
            "vertex-gemini-3.1-flash-image-preview",
        )
        self.assertEqual(
            upstream_client.calls[0]["headers"]["authorization"],
            "Bearer gateway-key",
        )

    async def test_image_generation_without_model_uses_default_image_alias_on_direct_vertex_lane(self) -> None:
        self._set_model_delivery_lane("gemini-image", "vertex_direct")
        settings.vertex_api_key = "vertex-direct-key"
        settings.vertex_gemini_api_base = "https://aiplatform.googleapis.com/v1/publishers/google"

        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {
                                            "inlineData": {
                                                "mimeType": "image/png",
                                                "data": "abc",
                                            }
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                proxy_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ):
                response = await client.post(
                    "/v1/images/generations",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={"prompt": "A blue coin mascot", "n": 1, "size": "1024x1024"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(len(payload["data"]), 1)
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(
            upstream_client.calls[0]["url"],
            "https://aiplatform.googleapis.com/v1/publishers/google/models/gemini-3.1-flash-image-preview:generateContent",
        )
        self.assertEqual(upstream_client.calls[0]["headers"]["x-goog-api-key"], "vertex-direct-key")
        self.assertEqual(upstream_client.calls[0]["json"]["contents"][0]["role"], "user")
        self.assertEqual(upstream_client.calls[0]["json"]["contents"][0]["parts"][0]["text"], "A blue coin mascot")
        self.assertEqual(upstream_client.calls[0]["json"]["generationConfig"]["candidateCount"], 1)
        self.assertEqual(upstream_client.calls[0]["json"]["generationConfig"]["imageConfig"]["aspectRatio"], "1:1")

    async def test_image_generation_can_call_vertex_directly_when_vertex_key_is_configured(self) -> None:
        self._set_model_delivery_lane("gemini-image", "vertex_direct")
        settings.vertex_api_key = "vertex-direct-key"
        settings.vertex_gemini_api_base = "https://aiplatform.googleapis.com/v1/publishers/google"

        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {
                                            "inlineData": {
                                                "mimeType": "image/png",
                                                "data": "generated-1",
                                            }
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                proxy_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ):
                response = await client.post(
                    "/v1/images/generations",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={
                        "model": "gemini-image",
                        "prompt": "A clean blue coin mascot",
                        "n": 1,
                        "size": "1792x1024",
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["data"][0]["b64_json"], "generated-1")
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(
            upstream_client.calls[0]["url"],
            "https://aiplatform.googleapis.com/v1/publishers/google/models/gemini-3.1-flash-image-preview:generateContent",
        )
        self.assertEqual(upstream_client.calls[0]["headers"]["x-goog-api-key"], "vertex-direct-key")
        self.assertEqual(upstream_client.calls[0]["json"]["contents"][0]["role"], "user")
        self.assertEqual(upstream_client.calls[0]["json"]["contents"][0]["parts"][0]["text"], "A clean blue coin mascot")
        self.assertEqual(upstream_client.calls[0]["json"]["generationConfig"]["candidateCount"], 1)
        self.assertEqual(upstream_client.calls[0]["json"]["generationConfig"]["imageConfig"]["aspectRatio"], "16:9")

    async def test_image_generation_rejects_candidate_count_above_one_on_direct_vertex_lane(self) -> None:
        self._set_model_delivery_lane("gemini-image", "vertex_direct")
        settings.vertex_api_key = "vertex-direct-key"
        settings.vertex_gemini_api_base = "https://aiplatform.googleapis.com/v1/publishers/google"

        upstream_client = _RecordingClient([])

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                proxy_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ):
                response = await client.post(
                    "/v1/images/generations",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={"model": "gemini-image", "prompt": "A clean blue coin mascot", "n": 2},
                )

        self.assertEqual(response.status_code, 400, response.text)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "image_candidate_count_not_supported")
        self.assertEqual(len(upstream_client.calls), 0)

    async def test_image_generation_retries_transport_errors_on_direct_vertex_lane(self) -> None:
        self._set_model_delivery_lane("gemini-image", "vertex_direct")
        settings.vertex_api_key = "vertex-direct-key"
        settings.vertex_gemini_api_base = "https://aiplatform.googleapis.com/v1/publishers/google"

        upstream_client = _RecordingClient(
            [
                httpx.RemoteProtocolError("Server disconnected without sending a response."),
                httpx.RemoteProtocolError("Server disconnected without sending a response."),
                _FakeUpstreamResponse(
                    {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {
                                            "inlineData": {
                                                "mimeType": "image/png",
                                                "data": "generated-after-retry",
                                            }
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                ),
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                proxy_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ):
                response = await client.post(
                    "/v1/images/generations",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={"model": "gemini-image", "prompt": "Retry image generation", "n": 1, "size": "1024x1024"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["data"][0]["b64_json"], "generated-after-retry")
        self.assertEqual(len(upstream_client.calls), 3)

    async def test_image_edit_uses_gateway_lane_without_vertex_key(self) -> None:
        upstream_client = _RecordingStreamClient(
            [
                _FakeUpstreamResponse(
                    {
                        "created": 1774449999,
                        "data": [{"b64_json": "edited-by-gateway"}],
                    }
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                proxy_module,
                "get_image_stream_client",
                AsyncMock(return_value=upstream_client),
            ):
                response = await client.post(
                    "/v1/images/edits",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    data={"prompt": "Turn this into pixel art", "n": "1", "size": "1024x1024"},
                    files={"image": ("input.png", b"fake_image_data", "image/png")},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"][0]["b64_json"], "edited-by-gateway")
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(upstream_client.calls[0]["url"], "https://gateway.example/v1/images/edits")

    async def test_image_edit_prefers_gateway_lane_when_gateway_is_configured(self) -> None:
        settings.gateway_base_url = "https://gateway.example"
        settings.gateway_api_key = "gateway-key"

        upstream_client = _RecordingStreamClient(
            [
                _FakeUpstreamResponse(
                    {
                        "created": 1774449999,
                        "data": [{"b64_json": "edited-by-gateway"}],
                    }
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                proxy_module,
                "get_image_stream_client",
                AsyncMock(return_value=upstream_client),
            ):
                response = await client.post(
                    "/v1/images/edits",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    data={"model": "gemini-image", "prompt": "Turn this into pixel art", "n": "1", "size": "1024x1024"},
                    files={"image": ("input.png", b"fake_image_data", "image/png")},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"][0]["b64_json"], "edited-by-gateway")
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(
            upstream_client.calls[0]["url"],
            "https://gateway.example/v1/images/edits",
        )
        self.assertIn("multipart/form-data; boundary=", upstream_client.calls[0]["headers"]["content-type"])
        posted_body = upstream_client.calls[0]["content"].decode("utf-8", errors="replace")
        self.assertIn('name="model"', posted_body)
        self.assertIn("vertex-gemini-3.1-flash-image-preview", posted_body)
        self.assertEqual(
            upstream_client.calls[0]["headers"]["authorization"],
            "Bearer gateway-key",
        )

    async def test_image_edit_without_model_uses_default_image_alias_on_direct_vertex_lane(self) -> None:
        self._set_model_delivery_lane("gemini-image", "vertex_direct")
        settings.vertex_api_key = "vertex-direct-key"
        settings.vertex_gemini_api_base = "https://aiplatform.googleapis.com/v1/publishers/google"

        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {
                                            "inlineData": {
                                                "mimeType": "image/png",
                                                "data": "edited",
                                            }
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                proxy_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ):
                response = await client.post(
                    "/v1/images/edits",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    data={"prompt": "Turn this into pixel art", "n": "1", "size": "1024x1024"},
                    files={"image": ("input.png", b"fake_image_data", "image/png")},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["data"][0]["b64_json"], "edited")
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(
            upstream_client.calls[0]["url"],
            "https://aiplatform.googleapis.com/v1/publishers/google/models/gemini-3.1-flash-image-preview:generateContent",
        )
        self.assertEqual(upstream_client.calls[0]["json"]["contents"][0]["role"], "user")

    async def test_image_edit_can_call_vertex_directly_when_vertex_key_is_configured(self) -> None:
        self._set_model_delivery_lane("gemini-image", "vertex_direct")
        settings.vertex_api_key = "vertex-direct-key"
        settings.vertex_gemini_api_base = "https://aiplatform.googleapis.com/v1/publishers/google"

        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {
                                            "inlineData": {
                                                "mimeType": "image/png",
                                                "data": "edited-directly",
                                            }
                                        }
                                    ]
                                }
                            },
                            {
                                "content": {
                                    "parts": [
                                        {
                                            "inlineData": {
                                                "mimeType": "image/png",
                                                "data": "edited-directly-2",
                                            }
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                proxy_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ):
                response = await client.post(
                    "/v1/images/edits",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    data={"model": "gemini-image", "prompt": "Make it monochrome", "size": "1024x1024", "n": "1"},
                    files={"image": ("input.png", b"fake_image_data", "image/png")},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["data"][0]["b64_json"], "edited-directly")
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(
            upstream_client.calls[0]["url"],
            "https://aiplatform.googleapis.com/v1/publishers/google/models/gemini-3.1-flash-image-preview:generateContent",
        )
        self.assertEqual(upstream_client.calls[0]["headers"]["x-goog-api-key"], "vertex-direct-key")
        self.assertEqual(upstream_client.calls[0]["json"]["contents"][0]["role"], "user")
        self.assertEqual(upstream_client.calls[0]["json"]["contents"][0]["parts"][-1]["text"], "Make it monochrome")
        self.assertEqual(upstream_client.calls[0]["json"]["generationConfig"]["responseModalities"], ["IMAGE"])
        self.assertEqual(upstream_client.calls[0]["json"]["generationConfig"]["candidateCount"], 1)
        self.assertEqual(upstream_client.calls[0]["json"]["generationConfig"]["imageConfig"]["aspectRatio"], "1:1")

    async def test_image_edit_rejects_candidate_count_above_one_on_direct_vertex_lane(self) -> None:
        self._set_model_delivery_lane("gemini-image", "vertex_direct")
        settings.vertex_api_key = "vertex-direct-key"
        settings.vertex_gemini_api_base = "https://aiplatform.googleapis.com/v1/publishers/google"

        upstream_client = _RecordingClient([])

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                proxy_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ):
                response = await client.post(
                    "/v1/images/edits",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    data={"model": "gemini-image", "prompt": "Make it monochrome", "n": "2"},
                    files={"image": ("input.png", b"fake_image_data", "image/png")},
                )

        self.assertEqual(response.status_code, 400, response.text)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "image_candidate_count_not_supported")
        self.assertEqual(len(upstream_client.calls), 0)

    async def test_image_edit_requires_async_job_when_input_count_exceeds_sync_limit(self) -> None:
        upstream_client = _RecordingStreamClient([])

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                proxy_module,
                "get_image_stream_client",
                AsyncMock(return_value=upstream_client),
            ):
                response = await client.post(
                    "/v1/images/edits",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    data={"model": "gemini-image", "prompt": "Blend these references", "n": "1"},
                    files=[
                        ("image", ("input-1.png", b"fake_image_data_1", "image/png")),
                        ("image", ("input-2.png", b"fake_image_data_2", "image/png")),
                        ("image", ("input-3.png", b"fake_image_data_3", "image/png")),
                    ],
                )

        self.assertEqual(response.status_code, 400, response.text)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "image_job_required")
        self.assertIn("/v1/image-jobs/edits", payload["error"]["message"])
        self.assertEqual(len(upstream_client.calls), 0)

    async def test_image_edit_requires_async_job_when_gateway_sync_budget_exceeded(self) -> None:
        async def _hang(*args, **kwargs):
            await asyncio.sleep(2)
            raise AssertionError("sync image edit should have timed out first")

        settings.image_edit_sync_gateway_timeout_seconds = 1

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                proxy_module,
                "get_image_stream_client",
                AsyncMock(return_value=object()),
            ), patch.object(
                proxy_module,
                "_send_stream_request",
                AsyncMock(side_effect=_hang),
            ), patch.object(proxy_module.usage_buffer, "add", AsyncMock()) as add_usage:
                response = await client.post(
                    "/v1/images/edits",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    data={"model": "gemini-image", "prompt": "Turn this into pixel art", "n": "1"},
                    files={"image": ("input.png", b"fake_image_data", "image/png")},
                )

        self.assertEqual(response.status_code, 400, response.text)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "image_job_required")
        self.assertIn("/v1/image-jobs/edits", payload["error"]["message"])
        add_usage.assert_not_awaited()

    async def test_image_edit_rejects_mask_on_direct_vertex_lane(self) -> None:
        self._set_model_delivery_lane("gemini-image", "vertex_direct")
        settings.vertex_api_key = "vertex-direct-key"
        settings.vertex_gemini_api_base = "https://aiplatform.googleapis.com/v1/publishers/google"

        upstream_client = _RecordingClient([])

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                proxy_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ):
                response = await client.post(
                    "/v1/images/edits",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    data={"model": "gemini-image", "prompt": "Replace the background"},
                    files={
                        "image": ("input.png", b"fake_image_data", "image/png"),
                        "mask": ("mask.png", b"fake_mask_data", "image/png"),
                    },
                )

        self.assertEqual(response.status_code, 400, response.text)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "mask_not_supported")
        self.assertEqual(len(upstream_client.calls), 0)

    async def test_image_edit_retries_transport_errors_on_direct_vertex_lane(self) -> None:
        self._set_model_delivery_lane("gemini-image", "vertex_direct")
        settings.vertex_api_key = "vertex-direct-key"
        settings.vertex_gemini_api_base = "https://aiplatform.googleapis.com/v1/publishers/google"

        upstream_client = _RecordingClient(
            [
                httpx.RemoteProtocolError("Server disconnected without sending a response."),
                httpx.RemoteProtocolError("Server disconnected without sending a response."),
                _FakeUpstreamResponse(
                    {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {
                                            "inlineData": {
                                                "mimeType": "image/png",
                                                "data": "edited-after-retry",
                                            }
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                ),
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                proxy_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ):
                response = await client.post(
                    "/v1/images/edits",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    data={"model": "gemini-image", "prompt": "Recover after transport error"},
                    files={"image": ("input.png", b"fake_image_data", "image/png")},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["data"][0]["b64_json"], "edited-after-retry")
        self.assertEqual(len(upstream_client.calls), 3)

    async def test_direct_gemini_error_does_not_fall_back_to_legacy_lane(self) -> None:
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {"error": {"message": "upstream failed", "type": "server_error", "code": "500"}},
                    status_code=500,
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(openai_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                openai_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ):
                response = await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={
                        "model": "gemini-fast",
                        "messages": [{"role": "user", "content": "Reply with only: OK"}],
                    },
                )

        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(upstream_client.calls[0]["url"], "https://gateway.example/v1/responses")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "gemini-fast")

    async def test_embeddings_endpoint_uses_dedicated_azure_embedding_model(self) -> None:
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "object": "list",
                        "data": [
                            {
                                "object": "embedding",
                                "index": 0,
                                "embedding": [0.1, 0.2, 0.3],
                            }
                        ],
                        "model": "text-embedding-3-small",
                        "usage": {"prompt_tokens": 8, "total_tokens": 8},
                    }
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(openai_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                openai_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ):
                response = await client.post(
                    "/v1/embeddings",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={"model": "text-embedding-3-small", "input": "hello"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["model"], "text-embedding-3-small")
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(upstream_client.calls[0]["url"], "https://fallback.example/v1/embeddings")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "text-embedding-3-small")
        self.assertEqual(upstream_client.calls[0]["headers"]["api-key"], "fallback-key")

    async def test_embeddings_endpoint_defaults_to_dedicated_embedding_model(self) -> None:
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "object": "list",
                        "data": [
                            {
                                "object": "embedding",
                                "index": 0,
                                "embedding": [0.1, 0.2, 0.3],
                            }
                        ],
                        "model": "text-embedding-3-small",
                        "usage": {"prompt_tokens": 4, "total_tokens": 4},
                    }
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(openai_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                openai_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ):
                response = await client.post(
                    "/v1/embeddings",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={"input": "hello"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "text-embedding-3-small")

    async def test_models_endpoint_returns_curated_metadata(self) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/v1/models")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["object"], "list")
        self.assertEqual(
            [item["id"] for item in payload["data"]],
            [
                "gpt-5.4",
                "gpt-5",
                "gpt-5.1",
                "gpt-5.1-codex",
                "gpt-5.1-codex-mini",
                "gpt-5.1-codex-max",
                "gpt-5.2",
                "gpt-5.2-codex",
                "gpt-5.3-codex",
                "gpt-5.4-mini",
                "gpt-5-codex",
                "gpt-5-codex-mini",
                "text-embedding-3-small",
                "gemini-fast",
                "gemini-image",
            ],
        )
        self.assertEqual(payload["data"][0]["coincoin_provider"], "OpenAI")
        self.assertEqual(payload["data"][0]["coincoin_billable_sku"], "gpt-5.4")
        self.assertEqual(payload["data"][0]["coincoin_default_for"], ["text"])
        self.assertEqual(payload["data"][7]["coincoin_provider"], "OpenAI")
        self.assertEqual(payload["data"][7]["coincoin_billable_sku"], "gpt-5.2-codex")
        self.assertEqual(payload["data"][12]["coincoin_provider"], "OpenAI")
        self.assertEqual(payload["data"][12]["coincoin_provider_model"], "text-embedding-3-small")
        self.assertEqual(payload["data"][12]["coincoin_capabilities"], ["embeddings"])
        self.assertEqual(payload["data"][12]["coincoin_billable_sku"], "azure-text-embedding-3-small")
        self.assertEqual(payload["data"][12]["coincoin_default_for"], ["embedding"])
        self.assertEqual(payload["data"][12]["coincoin_delivery_lane"], "upstream_direct")
        self.assertEqual(payload["data"][13]["coincoin_provider"], "Google")
        self.assertEqual(payload["data"][13]["coincoin_provider_model"], "gemini-2.5-flash")
        self.assertEqual(payload["data"][13]["coincoin_capabilities"], ["chat/completions", "responses"])
        self.assertEqual(payload["data"][13]["coincoin_billable_sku"], "gemini-fast")
        self.assertEqual(payload["data"][13]["coincoin_delivery_lane"], "gateway")
        self.assertEqual(payload["data"][14]["coincoin_capabilities"], ["images/generations", "images/edits"])
        self.assertEqual(payload["data"][14]["coincoin_default_for"], ["image"])
        self.assertEqual(payload["data"][14]["coincoin_delivery_lane"], "gateway")

    async def test_model_detail_endpoint_returns_curated_metadata(self) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/v1/models/gemini-fast")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["id"], "gemini-fast")
        self.assertEqual(payload["object"], "model")
        self.assertEqual(payload["owned_by"], "google")
        self.assertEqual(payload["coincoin_provider"], "Google")
        self.assertEqual(payload["coincoin_provider_model"], "gemini-2.5-flash")
        self.assertEqual(payload["coincoin_capabilities"], ["chat/completions", "responses"])
        self.assertEqual(payload["coincoin_billable_sku"], "gemini-fast")
        self.assertEqual(payload["coincoin_routing_mode"], "direct")
        self.assertEqual(payload["coincoin_delivery_lane"], "gateway")

    async def test_model_detail_endpoint_returns_openai_error_for_unknown_model(self) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/v1/models/not-a-real-model")

        self.assertEqual(response.status_code, 404, response.text)
        payload = response.json()
        self.assertEqual(payload["error"]["type"], "invalid_request_error")
        self.assertEqual(payload["error"]["param"], "model")
        self.assertEqual(payload["error"]["code"], "model_not_found")


if __name__ == "__main__":
    unittest.main()
