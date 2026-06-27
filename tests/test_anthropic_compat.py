import json
import os
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("COINCOIN_DATABASE_URL", "mysql://test:test@127.0.0.1:3306/test")

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app.anthropic_compat as anthropic_module
import app.openai_compat as openai_module
import app.proxy as proxy_module
from app.channel_router import ModelChannelRouteSnapshot, ProviderChannelSnapshot, channel_router
from app.router import registry
from app.config import settings


class _FakeUpstreamResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = {"content-type": "application/json", **(headers or {})}

    def json(self):
        return self._payload

    async def aread(self):
        import json
        return json.dumps(self._payload).encode("utf-8")


class _RecordingClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


class _DelayedRecordingClient(_RecordingClient):
    def __init__(self, responses, *, delay_seconds=0.0, exc=None):
        super().__init__(responses)
        self.delay_seconds = delay_seconds
        self.exc = exc

    async def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.exc:
            raise self.exc
        return self.responses.pop(0)


class _FakeEventStreamResponse:
    def __init__(self, lines, status_code=200, headers=None, body=None):
        self._lines = list(lines)
        self.status_code = status_code
        self.headers = {"content-type": "text/event-stream", **(headers or {})}
        self._body = body
        self._closed = False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        if self._body is None:
            return "\n".join(self._lines).encode("utf-8")
        if isinstance(self._body, bytes):
            return self._body
        if isinstance(self._body, str):
            return self._body.encode("utf-8")
        return json.dumps(self._body).encode("utf-8")

    async def aclose(self):
        self._closed = True


class _FakeAnthropicEventStreamResponse(_FakeEventStreamResponse):
    async def aiter_lines(self):
        for event in self._lines:
            for line in event.splitlines():
                yield line
            yield ""


class _RecordingStreamClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def build_request(self, method, url, **kwargs):
        request = {"method": method, "url": url, **kwargs}
        self.calls.append(request)
        return request

    async def send(self, request, stream=False):
        self.calls.append({"send_request": request, "stream": stream})
        return self.responses.pop(0)


class AnthropicCompatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._originals = {
            "upstream_base_url": settings.upstream_base_url,
            "upstream_api_key": settings.upstream_api_key,
            "fixed_model": settings.fixed_model,
            "primary_auth_style": settings.primary_auth_style,
            "router_enabled": settings.router_enabled,
            "claude_compat_provider": settings.claude_compat_provider,
            "claude_compat_base_url": settings.claude_compat_base_url,
            "claude_compat_api_key": settings.claude_compat_api_key,
            "claude_compat_auth_style": settings.claude_compat_auth_style,
        }
        settings.upstream_base_url = "https://cliproxyapi-deploy-production.up.railway.app/v1"
        settings.upstream_api_key = "sk-cliproxy-test"
        settings.fixed_model = "gpt-5.5"
        settings.primary_auth_style = "bearer"
        settings.router_enabled = False
        settings.claude_compat_provider = "upstream_direct"
        channel_router.clear_snapshot()
        registry.init_from_settings()
        self.app = FastAPI()
        self.app.include_router(anthropic_module.router)
        self.app.include_router(openai_module.router)

    def tearDown(self):
        channel_router.clear_snapshot()
        for key, value in self._originals.items():
            setattr(settings, key, value)
        registry._initialized = False

    def _configure_anthropic_compatible_channel(self, *, public_model_id="claude-opus-4-7", upstream_model="claude-fable-5"):
        channel_router.set_snapshot(
            [
                ProviderChannelSnapshot(
                    channel_id="ch_anthropic_compat",
                    name="Claude relay",
                    provider_platform="claude_relay",
                    channel_type="anthropic_compatible",
                    base_url="https://claude-relay.example",
                    api_key="relay-key",
                    auth_style="x-api-key",
                    priority=0,
                    capabilities=("chat/completions",),
                )
            ],
            [
                ModelChannelRouteSnapshot(
                    route_id="mcr_anthropic_compat",
                    public_model_id=public_model_id,
                    endpoint="chat/completions",
                    channel_id="ch_anthropic_compat",
                    upstream_model=upstream_model,
                    transform_profile="anthropic_messages",
                )
            ],
        )

    async def test_models_returns_claude_shape_for_claude_cli(self):
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as client:
            response = await client.get("/v1/models", headers={"user-agent": "claude-cli/2.0.76 (external, cli)"})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("data", data)
        self.assertIsInstance(data["data"], list)
        self.assertTrue(all("display_name" in item for item in data["data"]))

    async def test_count_tokens_accepts_claude_payload(self):
        fake_user = SimpleNamespace(id="u_test", status="active")
        with patch.object(anthropic_module, "authenticate_user", AsyncMock(return_value=fake_user)):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as client:
                response = await client.post(
                    "/v1/messages/count_tokens",
                    headers={"authorization": "Bearer sk_test", "anthropic-version": "2023-06-01"},
                    json={
                        "model": "gpt-5.5",
                        "messages": [{"role": "user", "content": "hello world"}],
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["input_tokens"], 2)

    async def test_messages_forwards_to_cpa_and_returns_anthropic_shape(self):
        fake_user = SimpleNamespace(id="u_test", status="active", _api_key_id="k_claude")
        client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "chatcmpl_test",
                        "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                        "usage": {
                            "prompt_tokens": 12,
                            "completion_tokens": 3,
                            "total_tokens": 15,
                            "prompt_tokens_details": {"cached_tokens": 7},
                        },
                    }
                )
            ]
        )

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_http_client", AsyncMock(return_value=client)),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()) as add_usage,
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={
                        "authorization": "Bearer sk_test",
                        "anthropic-version": "2023-06-01",
                        "user-agent": "claude-cli/2.0.76 (external, cli)",
                    },
                    json={
                        "model": "gpt-5.5",
                        "max_tokens": 64,
                        "messages": [{"role": "user", "content": "Reply with exactly OK"}],
                    },
                )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], "message")
        self.assertEqual(body["model"], "gpt-5.5")
        self.assertEqual(body["content"][0]["text"], "OK")
        self.assertEqual(body["usage"]["input_tokens"], 5)
        self.assertEqual(body["usage"]["cache_read_input_tokens"], 7)
        self.assertEqual(client.calls[0]["url"], "https://cliproxyapi-deploy-production.up.railway.app/v1/chat/completions")
        self.assertEqual(client.calls[0]["json"]["model"], "gpt-5.5")
        self.assertNotIn("prompt_cache_key", client.calls[0]["json"])
        add_usage.assert_awaited_once()
        usage_kwargs = add_usage.await_args.kwargs
        self.assertEqual(usage_kwargs["api_key_id"], "k_claude")
        self.assertEqual(usage_kwargs["cache_read_tokens"], 7)
        self.assertEqual(usage_kwargs["cache_creation_tokens"], 0)
        self.assertGreater(usage_kwargs["duration_ms"], 0)

    async def test_messages_normalizes_root_openai_compatible_base_url(self):
        settings.upstream_base_url = "https://sub2api.example"
        registry.init_from_settings()
        fake_user = SimpleNamespace(id="u_test", status="active", _api_key_id="k_claude_root")
        client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "chatcmpl_root",
                        "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
                    }
                )
            ]
        )

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_http_client", AsyncMock(return_value=client)),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={"authorization": "Bearer sk_test", "anthropic-version": "2023-06-01"},
                    json={
                        "model": "gpt-5.5",
                        "max_tokens": 64,
                        "messages": [{"role": "user", "content": "Reply with exactly OK"}],
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.calls[0]["url"], "https://sub2api.example/v1/chat/completions")

    async def test_messages_subtract_cache_creation_from_anthropic_input_tokens(self):
        fake_user = SimpleNamespace(id="u_test", status="active", _api_key_id="k_claude")
        client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "chatcmpl_cache_write",
                        "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                        "usage": {
                            "input_tokens": 5,
                            "completion_tokens": 2,
                            "cache_read_input_tokens": 7,
                            "cache_creation_input_tokens": 8,
                        },
                    }
                )
            ]
        )

        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
            with patch("app.anthropic_compat.authorize_request", AsyncMock(return_value=fake_user)), patch(
                "app.anthropic_compat.get_http_client", AsyncMock(return_value=client)
            ), patch("app.anthropic_compat.usage_buffer.add", AsyncMock()) as add_usage:
                response = await ac.post(
                    "/v1/messages",
                    headers={"Authorization": "Bearer sk_test"},
                    json={
                        "model": "gpt-5.5",
                        "messages": [{"role": "user", "content": "hi"}],
                        "max_tokens": 16,
                    },
                )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["usage"]["input_tokens"], 5)
        self.assertEqual(body["usage"]["cache_read_input_tokens"], 7)
        self.assertEqual(body["usage"]["cache_creation_input_tokens"], 8)
        usage_kwargs = add_usage.await_args.kwargs
        self.assertEqual(usage_kwargs["input_tokens"], 20)
        self.assertEqual(usage_kwargs["cache_read_tokens"], 7)
        self.assertEqual(usage_kwargs["cache_creation_tokens"], 8)

    async def test_claude_alias_resolves_to_gpt_55_upstream(self):
        fake_user = SimpleNamespace(id="u_test", status="active", _api_key_id="k_alias")
        client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "chatcmpl_alias",
                        "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                        "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
                    }
                )
            ]
        )

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_http_client", AsyncMock(return_value=client)),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={
                        "authorization": "Bearer sk_test",
                        "anthropic-version": "2023-06-01",
                        "user-agent": "claude-cli/2.0.76 (external, cli)",
                    },
                    json={
                        "model": "claude-opus-4-7",
                        "max_tokens": 64,
                        "messages": [{"role": "user", "content": "Reply with exactly OK"}],
                    },
                )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["model"], "claude-opus-4-7")
        self.assertEqual(client.calls[0]["json"]["model"], "gpt-5.5")
        prompt_cache_key = client.calls[0]["json"].get("prompt_cache_key", "")
        self.assertTrue(prompt_cache_key.startswith("cc-"))
        self.assertEqual(len(prompt_cache_key), 35)

    async def test_messages_routes_anthropic_compatible_channel_to_native_messages(self):
        self._configure_anthropic_compatible_channel()
        registry._initialized = False
        registry.init_from_settings()
        fake_user = SimpleNamespace(id="u_test", status="active", _api_key_id="k_anthropic_channel")
        client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "msg_relay_native",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-fable-5",
                        "content": [{"type": "text", "text": "OK"}],
                        "stop_reason": "end_turn",
                        "stop_sequence": None,
                        "usage": {"input_tokens": 8, "output_tokens": 2},
                    }
                )
            ]
        )

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_http_client", AsyncMock(return_value=client)),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()) as add_usage,
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={
                        "authorization": "Bearer sk_test",
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": "claude-opus-4-7",
                        "max_tokens": 64,
                        "system": "You are concise.",
                        "messages": [{"role": "user", "content": [{"type": "text", "text": "Reply OK"}]}],
                        "tools": [
                            {
                                "name": "Read",
                                "description": "Read a file",
                                "input_schema": {
                                    "type": "object",
                                    "properties": {"path": {"type": "string"}},
                                    "required": ["path"],
                                },
                            }
                        ],
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["model"], "claude-opus-4-7")
        self.assertEqual(body["content"][0]["text"], "OK")
        self.assertEqual(client.calls[0]["url"], "https://claude-relay.example/v1/messages")
        self.assertEqual(client.calls[0]["json"]["model"], "claude-fable-5")
        self.assertEqual(client.calls[0]["json"]["system"], "You are concise.")
        self.assertEqual(client.calls[0]["json"]["tools"][0]["name"], "Read")
        self.assertEqual(client.calls[0]["headers"]["x-api-key"], "relay-key")
        self.assertEqual(client.calls[0]["headers"]["anthropic-version"], "2023-06-01")
        self.assertNotIn("authorization", client.calls[0]["headers"])
        add_usage.assert_awaited_once()
        usage_kwargs = add_usage.await_args.kwargs
        self.assertEqual(usage_kwargs["provider_model"], "claude-fable-5")
        self.assertEqual(usage_kwargs["channel_type"], "anthropic_compatible")

    async def test_chat_completions_routes_to_anthropic_compatible_channel_and_returns_openai_shape(self):
        self._configure_anthropic_compatible_channel()
        registry._initialized = False
        registry.init_from_settings()
        fake_user = SimpleNamespace(id="u_test", status="active", _api_key_id="k_chat_anthropic")
        client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "msg_relay_chat",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-fable-5",
                        "content": [
                            {"type": "text", "text": "Using tool."},
                            {
                                "type": "tool_use",
                                "id": "toolu_1",
                                "name": "Read",
                                "input": {"path": "README.md"},
                            },
                        ],
                        "stop_reason": "tool_use",
                        "stop_sequence": None,
                        "usage": {
                            "input_tokens": 11,
                            "output_tokens": 5,
                            "cache_read_input_tokens": 3,
                            "cache_creation": {"ephemeral_5m_input_tokens": 2},
                        },
                    }
                )
            ]
        )

        with (
            patch.object(openai_module, "authorize_workbench_request", AsyncMock(return_value=fake_user)),
            patch.object(proxy_module, "authorize_workbench_request", AsyncMock(return_value=fake_user)),
            patch.object(openai_module, "get_http_client", AsyncMock(return_value=client)),
            patch.object(openai_module.usage_buffer, "add", AsyncMock()) as add_usage,
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/chat/completions",
                    headers={"authorization": "Bearer sk_test"},
                    json={
                        "model": "claude-opus-4-7",
                        "messages": [
                            {"role": "system", "content": "You are concise."},
                            {"role": "user", "content": "Read the README"},
                        ],
                        "max_tokens": 64,
                        "temperature": 0.2,
                        "tools": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "Read",
                                    "description": "Read a file",
                                    "parameters": {
                                        "type": "object",
                                        "properties": {"path": {"type": "string"}},
                                        "required": ["path"],
                                    },
                                },
                            }
                        ],
                        "tool_choice": {"type": "function", "function": {"name": "Read"}},
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["object"], "chat.completion")
        self.assertEqual(body["model"], "claude-opus-4-7")
        message = body["choices"][0]["message"]
        self.assertEqual(message["content"], "Using tool.")
        self.assertEqual(message["tool_calls"][0]["id"], "toolu_1")
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "Read")
        self.assertEqual(message["tool_calls"][0]["function"]["arguments"], '{"path":"README.md"}')
        self.assertEqual(body["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(body["usage"]["prompt_tokens"], 16)
        self.assertEqual(body["usage"]["total_tokens"], 21)
        self.assertEqual(body["usage"]["prompt_tokens_details"]["cached_tokens"], 3)
        upstream_payload = client.calls[0]["json"]
        self.assertEqual(client.calls[0]["url"], "https://claude-relay.example/v1/messages")
        self.assertEqual(upstream_payload["model"], "claude-fable-5")
        self.assertEqual(upstream_payload["system"], "You are concise.")
        self.assertEqual(upstream_payload["messages"], [{"role": "user", "content": "Read the README"}])
        self.assertEqual(upstream_payload["tools"][0]["input_schema"]["properties"]["path"]["type"], "string")
        self.assertEqual(upstream_payload["tool_choice"], {"type": "tool", "name": "Read"})
        self.assertEqual(client.calls[0]["headers"]["x-api-key"], "relay-key")
        add_usage.assert_awaited_once()
        self.assertEqual(add_usage.await_args.kwargs["channel_type"], "anthropic_compatible")

    async def test_chat_completions_stream_translates_anthropic_compatible_sse(self):
        self._configure_anthropic_compatible_channel()
        registry._initialized = False
        registry.init_from_settings()
        fake_user = SimpleNamespace(id="u_test", status="active", _api_key_id="k_chat_anthropic_stream")
        stream_client = _RecordingStreamClient(
            [
                _FakeAnthropicEventStreamResponse(
                    [
                        'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_stream_relay","type":"message","role":"assistant","model":"claude-fable-5","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":7,"output_tokens":0}}}',
                        'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
                        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"OK"}}',
                        'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}',
                        'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":7,"output_tokens":2}}',
                        'event: message_stop\ndata: {"type":"message_stop"}',
                    ]
                )
            ]
        )

        with (
            patch.object(openai_module, "authorize_workbench_request", AsyncMock(return_value=fake_user)),
            patch.object(proxy_module, "authorize_workbench_request", AsyncMock(return_value=fake_user)),
            patch.object(openai_module, "get_stream_client", AsyncMock(return_value=stream_client)),
            patch.object(openai_module.usage_buffer, "add", AsyncMock()) as add_usage,
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/chat/completions",
                    headers={"authorization": "Bearer sk_test"},
                    json={
                        "model": "claude-opus-4-7",
                        "messages": [{"role": "user", "content": "Stream OK"}],
                        "max_tokens": 64,
                        "stream": True,
                        "stream_options": {"include_usage": True},
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.text
        self.assertIn('"object": "chat.completion.chunk"', body)
        self.assertIn('"model": "claude-opus-4-7"', body)
        self.assertIn('"content": "OK"', body)
        self.assertIn('"finish_reason": "stop"', body)
        self.assertIn('"usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}', body)
        self.assertIn("data: [DONE]", body)
        self.assertEqual(stream_client.calls[0]["url"], "https://claude-relay.example/v1/messages")
        self.assertEqual(stream_client.calls[0]["json"]["model"], "claude-fable-5")
        self.assertTrue(stream_client.calls[0]["json"]["stream"])
        self.assertEqual(stream_client.calls[0]["headers"]["x-api-key"], "relay-key")
        add_usage.assert_awaited_once()
        usage_kwargs = add_usage.await_args.kwargs
        self.assertEqual(usage_kwargs["input_tokens"], 7)
        self.assertEqual(usage_kwargs["output_tokens"], 2)
        self.assertEqual(usage_kwargs["channel_type"], "anthropic_compatible")

    async def test_claude_alias_user_override_preserves_public_alias_and_applies_cache_billing_override(self):
        fake_user = SimpleNamespace(
            id="u_test",
            status="active",
            _api_key_id="k_alias_override",
            _model_routing_overrides={
                "claude-opus-4-7": {
                    "public_model_id": "claude-opus-4-7",
                    "provider_model": "gpt-5.4-mini",
                    "upstream_model": "gpt-5.4-mini",
                    "enabled": True,
                }
            },
            _model_pricing_overrides={
                "claude-opus-4-7": {
                    "public_model_id": "claude-opus-4-7",
                    "cache_read_multiplier_override": 1.0,
                }
            },
        )
        client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "chatcmpl_alias_override",
                        "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                        "usage": {
                            "prompt_tokens": 12,
                            "prompt_tokens_details": {"cached_tokens": 7},
                            "completion_tokens": 4,
                            "total_tokens": 16,
                        },
                    }
                )
            ]
        )

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_http_client", AsyncMock(return_value=client)),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()) as add_usage,
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={
                        "authorization": "Bearer sk_test",
                        "anthropic-version": "2023-06-01",
                        "user-agent": "claude-cli/2.0.76 (external, cli)",
                    },
                    json={
                        "model": "claude-opus-4-7",
                        "max_tokens": 64,
                        "messages": [{"role": "user", "content": "Reply with exactly OK"}],
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["model"], "claude-opus-4-7")
        self.assertEqual(client.calls[0]["json"]["model"], "gpt-5.4-mini")
        add_usage.assert_awaited_once()
        usage_kwargs = add_usage.await_args.kwargs
        self.assertEqual(usage_kwargs["customer_model_alias"], "claude-opus-4-7")
        self.assertEqual(usage_kwargs["provider_model"], "gpt-5.4-mini")
        self.assertEqual(usage_kwargs["cache_read_multiplier"], 1.0)
        self.assertEqual(usage_kwargs["effective_cached_input_per_million"], 500.0)

    async def test_messages_can_route_to_kiro_go_native_messages_path(self):
        fake_user = SimpleNamespace(id="u_test", status="active", _api_key_id="k_kiro_native")
        settings.claude_compat_provider = "kiro_go"
        settings.claude_compat_base_url = "https://kiro-go.example"
        settings.claude_compat_api_key = "kiro-key"
        settings.claude_compat_auth_style = "bearer"
        registry._initialized = False
        registry.init_from_settings()
        client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "chatcmpl_kiro_native",
                        "object": "chat.completion",
                        "created": 1700000000,
                        "model": "claude-opus-4.7",
                        "choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 14, "prompt_tokens_details": {"cached_tokens": 7}},
                    }
                )
            ]
        )

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_http_client", AsyncMock(return_value=client)),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()) as add_usage,
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={
                        "authorization": "Bearer sk_test",
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": "claude-opus-4-7",
                        "max_tokens": 64,
                        "messages": [{"role": "user", "content": "Reply with exactly OK"}],
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["model"], "claude-opus-4-7")
        self.assertEqual(body["content"][0]["text"], "OK")
        self.assertEqual(body["usage"]["cache_read_input_tokens"], 7)
        self.assertEqual(client.calls[0]["url"], "https://kiro-go.example/v1/messages")
        self.assertEqual(client.calls[0]["json"]["model"], "claude-opus-4.7")
        self.assertEqual(client.calls[0]["json"]["messages"][-1], {"role": "user", "content": "Reply with exactly OK"})
        self.assertEqual(client.calls[0]["headers"]["authorization"], "Bearer kiro-key")
        self.assertNotIn("prompt_cache_key", client.calls[0]["json"])
        add_usage.assert_awaited_once()
        self.assertEqual(add_usage.await_args.kwargs["cache_read_tokens"], 7)

    async def test_messages_can_passthrough_kiro_go_native_anthropic_message(self):
        fake_user = SimpleNamespace(id="u_test", status="active", _api_key_id="k_kiro_native_message")
        settings.claude_compat_provider = "kiro_go"
        settings.claude_compat_base_url = "https://kiro-go.example"
        settings.claude_compat_api_key = "kiro-key"
        settings.claude_compat_auth_style = "bearer"
        registry._initialized = False
        registry.init_from_settings()
        client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "msg_kiro_native",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-opus-4.7",
                        "content": [{"type": "text", "text": "OK"}],
                        "stop_reason": "end_turn",
                        "stop_sequence": None,
                        "usage": {
                            "input_tokens": 5,
                            "output_tokens": 2,
                            "cache_read_input_tokens": 7,
                            "cache_creation_input_tokens": 8,
                        },
                    }
                )
            ]
        )

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_http_client", AsyncMock(return_value=client)),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()) as add_usage,
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={
                        "authorization": "Bearer sk_test",
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": "claude-opus-4-7",
                        "max_tokens": 64,
                        "cache_control": {"type": "ephemeral"},
                        "messages": [{"role": "user", "content": "Reply with exactly OK"}],
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["model"], "claude-opus-4-7")
        self.assertEqual(body["usage"]["input_tokens"], 5)
        self.assertEqual(body["usage"]["cache_read_input_tokens"], 7)
        self.assertEqual(body["usage"]["cache_creation_input_tokens"], 8)
        self.assertEqual(client.calls[0]["url"], "https://kiro-go.example/v1/messages")
        self.assertEqual(client.calls[0]["json"]["cache_control"], {"type": "ephemeral"})
        add_usage.assert_awaited_once()
        usage_kwargs = add_usage.await_args.kwargs
        self.assertEqual(usage_kwargs["input_tokens"], 20)
        self.assertEqual(usage_kwargs["cache_read_tokens"], 7)
        self.assertEqual(usage_kwargs["cache_creation_tokens"], 8)

    async def test_messages_canonicalizes_kiro_go_model_at_final_hop(self):
        fake_user = SimpleNamespace(id="u_test", status="active", _api_key_id="k_kiro_canonical")
        settings.claude_compat_provider = "kiro_go"
        settings.claude_compat_base_url = "https://kiro-go.example"
        settings.claude_compat_api_key = "kiro-key"
        settings.claude_compat_auth_style = "bearer"
        registry.set_runtime_alias_overrides(
            {
                "claude-opus-4-7": {
                    "provider_model": "claude-opus-4-7",
                    "upstream_model": "claude-opus-4-7",
                }
            },
            version=1,
        )
        registry.init_from_settings()
        client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "chatcmpl_kiro_canonical",
                        "object": "chat.completion",
                        "created": 1700000000,
                        "model": "claude-opus-4.7",
                        "choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
                    }
                )
            ]
        )

        try:
            with (
                patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
                patch.object(anthropic_module, "get_http_client", AsyncMock(return_value=client)),
                patch.object(anthropic_module.usage_buffer, "add", AsyncMock()),
            ):
                async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                    response = await http_client.post(
                        "/v1/messages",
                        headers={
                            "authorization": "Bearer sk_test",
                            "anthropic-version": "2023-06-01",
                        },
                        json={
                            "model": "claude-opus-4-7",
                            "max_tokens": 64,
                            "messages": [
                                {"role": "user", "content": "Read foo.txt"},
                                {
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "tool_use",
                                            "id": "call_read_1",
                                            "name": "Read",
                                            "input": {"file_path": "foo.txt"},
                                        }
                                    ],
                                },
                                {
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "tool_result",
                                            "tool_use_id": "call_read_1",
                                            "content": "file contents",
                                        }
                                    ],
                                },
                            ],
                            "tools": [
                                {
                                    "name": "Read",
                                    "description": "Read a file",
                                    "input_schema": {
                                        "type": "object",
                                        "properties": {"file_path": {"type": "string"}},
                                    },
                                }
                            ],
                        },
                    )
        finally:
            registry.clear_runtime_alias_overrides()
            registry.init_from_settings()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["model"], "claude-opus-4-7")
        self.assertEqual(client.calls[0]["url"], "https://kiro-go.example/v1/messages")
        self.assertEqual(client.calls[0]["json"]["model"], "claude-opus-4.7")
        upstream_messages = client.calls[0]["json"]["messages"]
        self.assertEqual(upstream_messages[1]["content"][0]["name"], "Read")
        self.assertEqual(
            upstream_messages[2],
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_read_1",
                        "content": "file contents",
                    }
                ],
            },
        )

    async def test_messages_kiro_go_skips_model_cloak_injection(self):
        fake_user = SimpleNamespace(id="u_test", status="active", _api_key_id="k_kiro_cloak")
        settings.claude_compat_provider = "kiro_go"
        settings.claude_compat_base_url = "https://kiro-go.example"
        settings.claude_compat_api_key = "kiro-key"
        settings.claude_compat_auth_style = "bearer"
        settings.model_cloak = True
        registry._initialized = False
        registry.init_from_settings()
        client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "chatcmpl_kiro_cloak",
                        "object": "chat.completion",
                        "created": 1700000000,
                        "model": "claude-opus-4.7",
                        "choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
                    }
                )
            ]
        )

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_http_client", AsyncMock(return_value=client)),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={
                        "authorization": "Bearer sk_test",
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": "claude-opus-4-7",
                        "max_tokens": 64,
                        "messages": [{"role": "user", "content": "Reply with exactly OK"}],
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        upstream_messages = client.calls[0]["json"]["messages"]
        self.assertEqual(upstream_messages, [{"role": "user", "content": "Reply with exactly OK"}])

    async def test_messages_stream_can_route_to_kiro_go_native_messages_path(self):
        fake_user = SimpleNamespace(id="u_test", status="active", _api_key_id="k_kiro_stream")
        settings.claude_compat_provider = "kiro_go"
        settings.claude_compat_base_url = "https://kiro-go.example"
        settings.claude_compat_api_key = "kiro-key"
        settings.claude_compat_auth_style = "bearer"
        registry._initialized = False
        registry.init_from_settings()
        stream_client = _RecordingStreamClient(
            [
                _FakeEventStreamResponse(
                    [
                        'data: {"id":"chatcmpl_kiro_stream","model":"claude-opus-4.7","choices":[{"delta":{"content":"OK"},"finish_reason":null}]}',
                        'data: {"id":"chatcmpl_kiro_stream","model":"claude-opus-4.7","choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":14,"prompt_tokens_details":{"cached_tokens":7}}}',
                        "data: [DONE]",
                    ]
                ),
            ]
        )

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_stream_client", AsyncMock(return_value=stream_client)),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()) as add_usage,
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={
                        "authorization": "Bearer sk_test",
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": "claude-opus-4-7",
                        "stream": True,
                        "max_tokens": 64,
                        "messages": [{"role": "user", "content": "Reply with exactly OK"}],
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("event: message_start", response.text)
        self.assertIn('"model":"claude-opus-4-7"', response.text)
        self.assertIn('"type":"text_delta"', response.text)
        self.assertIn("OK", response.text)
        self.assertIn("event: message_delta", response.text)
        self.assertIn("event: message_stop", response.text)
        self.assertIn('"cache_read_input_tokens":7', response.text)
        self.assertEqual(stream_client.calls[0]["url"], "https://kiro-go.example/v1/messages")
        self.assertEqual(stream_client.calls[0]["json"]["model"], "claude-opus-4.7")
        self.assertTrue(stream_client.calls[0]["json"]["stream"])
        add_usage.assert_awaited_once()
        self.assertEqual(add_usage.await_args.kwargs["cache_read_tokens"], 7)

    async def test_messages_stream_can_passthrough_kiro_go_native_anthropic_sse(self):
        fake_user = SimpleNamespace(id="u_test", status="active", _api_key_id="k_kiro_stream_native")
        settings.claude_compat_provider = "kiro_go"
        settings.claude_compat_base_url = "https://kiro-go.example"
        settings.claude_compat_api_key = "kiro-key"
        settings.claude_compat_auth_style = "bearer"
        registry._initialized = False
        registry.init_from_settings()
        stream_client = _RecordingStreamClient(
            [
                _FakeAnthropicEventStreamResponse(
                    [
                        'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_native_stream","type":"message","role":"assistant","model":"claude-opus-4.7","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":5,"cache_creation_input_tokens":8,"cache_read_input_tokens":7,"output_tokens":0}}}',
                        'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
                        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"OK"}}',
                        'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}',
                        'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":5,"cache_creation_input_tokens":8,"cache_read_input_tokens":7,"output_tokens":2}}',
                        'event: message_stop\ndata: {"type":"message_stop"}',
                    ]
                ),
            ]
        )

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_stream_client", AsyncMock(return_value=stream_client)),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()) as add_usage,
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={
                        "authorization": "Bearer sk_test",
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": "claude-opus-4-7",
                        "stream": True,
                        "max_tokens": 64,
                        "cache_control": {"type": "ephemeral"},
                        "messages": [{"role": "user", "content": "Reply with exactly OK"}],
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.text
        self.assertIn("event: message_start", body)
        self.assertEqual(body.count("event: message_start"), 1)
        self.assertIn('"model":"claude-opus-4-7"', body)
        self.assertNotIn('"model":"claude-opus-4.7"', body)
        self.assertIn('"input_tokens":5', body)
        self.assertIn('"cache_read_input_tokens":7', body)
        self.assertIn('"cache_creation_input_tokens":8', body)
        self.assertIn("event: message_delta", body)
        self.assertIn("event: message_stop", body)
        self.assertEqual(stream_client.calls[0]["url"], "https://kiro-go.example/v1/messages")
        self.assertEqual(stream_client.calls[0]["json"]["cache_control"], {"type": "ephemeral"})
        add_usage.assert_awaited_once()
        usage_kwargs = add_usage.await_args.kwargs
        self.assertEqual(usage_kwargs["input_tokens"], 20)
        self.assertEqual(usage_kwargs["cache_read_tokens"], 7)
        self.assertEqual(usage_kwargs["cache_creation_tokens"], 8)

    async def test_messages_stream_kiro_go_native_anthropic_sse_keeps_upstream_usage_in_message_start(self):
        fake_user = SimpleNamespace(id="u_test", status="active", _api_key_id="k_kiro_stream_native_usage")
        settings.claude_compat_provider = "kiro_go"
        settings.claude_compat_base_url = "https://kiro-go.example"
        settings.claude_compat_api_key = "kiro-key"
        settings.claude_compat_auth_style = "bearer"
        registry._initialized = False
        registry.init_from_settings()
        stream_client = _RecordingStreamClient(
            [
                _FakeAnthropicEventStreamResponse(
                    [
                        'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_native_stream_usage","type":"message","role":"assistant","model":"claude-opus-4.7","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":5,"cache_creation_input_tokens":8,"cache_read_input_tokens":7,"output_tokens":0}}}',
                        'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
                        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"OK"}}',
                        'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}',
                        'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":5,"cache_creation_input_tokens":8,"cache_read_input_tokens":7,"output_tokens":2}}',
                        'event: message_stop\ndata: {"type":"message_stop"}',
                    ]
                ),
            ]
        )

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_stream_client", AsyncMock(return_value=stream_client)),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={
                        "authorization": "Bearer sk_test",
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": "claude-opus-4-7",
                        "stream": True,
                        "max_tokens": 64,
                        "cache_control": {"type": "ephemeral"},
                        "messages": [{"role": "user", "content": "Reply with exactly OK"}],
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.text
        self.assertIn(
            '"usage":{"input_tokens":5,"cache_creation_input_tokens":8,"cache_read_input_tokens":7,"output_tokens":0}',
            body,
        )
        self.assertEqual(body.count("event: message_start"), 1)

    async def test_messages_stream_kiro_go_bridge_sends_start_and_ping_while_waiting(self):
        fake_user = SimpleNamespace(id="u_test", status="active", _api_key_id="k_kiro_stream_ping")
        settings.claude_compat_provider = "kiro_go"
        settings.claude_compat_base_url = "https://kiro-go.example"
        settings.claude_compat_api_key = "kiro-key"
        settings.claude_compat_auth_style = "bearer"
        registry._initialized = False
        registry.init_from_settings()

        class _SlowEventStreamResponse(_FakeEventStreamResponse):
            async def aiter_lines(self):
                yield 'data: {"id":"chatcmpl_kiro_delayed","model":"claude-opus-4.7","choices":[{"delta":{"content":"OK"},"finish_reason":null}]}'
                await asyncio.sleep(0.03)
                yield 'data: {"id":"chatcmpl_kiro_delayed","model":"claude-opus-4.7","choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}'
                yield "data: [DONE]"

        stream_client = _RecordingStreamClient(
            [
                _SlowEventStreamResponse([])
            ]
        )

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_stream_client", AsyncMock(return_value=stream_client)),
            patch.object(anthropic_module, "_kiro_go_bridge_ping_interval", return_value=0.01),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={
                        "authorization": "Bearer sk_test",
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": "claude-opus-4-7",
                        "stream": True,
                        "max_tokens": 64,
                        "messages": [
                            {"role": "user", "content": "Read a large file"},
                            {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": "call_read_1",
                                        "name": "Read",
                                        "input": {"file_path": "big.txt"},
                                    }
                                ],
                            },
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": "call_read_1",
                                        "content": "large file contents",
                                    }
                                ],
                            },
                        ],
                        "tools": [
                            {
                                "name": "Read",
                                "description": "Read a file",
                                "input_schema": {
                                    "type": "object",
                                    "properties": {"file_path": {"type": "string"}},
                                },
                            }
                        ],
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.text
        message_start_index = body.find("event: message_start")
        ping_index = body.find("event: ping")
        text_delta_index = body.find('"type":"text_delta"')
        message_stop_index = body.find("event: message_stop")
        self.assertGreaterEqual(message_start_index, 0)
        self.assertGreater(ping_index, message_start_index)
        self.assertGreater(text_delta_index, message_start_index)
        self.assertGreater(message_stop_index, ping_index)
        self.assertIn("event: message_stop", body)
        self.assertEqual(
            stream_client.calls[0]["json"]["messages"][2]["content"][0]["content"],
            "large file contents",
        )
        self.assertGreaterEqual(stream_client.calls[0]["timeout"].read, 300)

    async def test_messages_stream_kiro_go_bridge_returns_sse_error_after_start(self):
        fake_user = SimpleNamespace(id="u_test", status="active", _api_key_id="k_kiro_stream_error")
        settings.claude_compat_provider = "kiro_go"
        settings.claude_compat_base_url = "https://kiro-go.example"
        settings.claude_compat_api_key = "kiro-key"
        settings.claude_compat_auth_style = "bearer"
        registry._initialized = False
        registry.init_from_settings()
        stream_client = _RecordingStreamClient([])
        stream_client.send = AsyncMock(side_effect=anthropic_module.httpx.ReadTimeout("boom"))

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_stream_client", AsyncMock(return_value=stream_client)),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()) as add_usage,
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={
                        "authorization": "Bearer sk_test",
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": "claude-opus-4-7",
                        "stream": True,
                        "max_tokens": 64,
                        "messages": [{"role": "user", "content": "Reply with exactly OK"}],
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.text
        self.assertIn("event: error", body)
        self.assertIn('"type":"api_error"', body)
        self.assertNotIn("event: message_stop", body)
        add_usage.assert_not_awaited()

    async def test_messages_stream_kiro_go_tool_call_finishes_with_message_stop(self):
        fake_user = SimpleNamespace(id="u_test", status="active", _api_key_id="k_kiro_stream_tool")
        settings.claude_compat_provider = "kiro_go"
        settings.claude_compat_base_url = "https://kiro-go.example"
        settings.claude_compat_api_key = "kiro-key"
        settings.claude_compat_auth_style = "bearer"
        registry._initialized = False
        registry.init_from_settings()
        stream_client = _RecordingStreamClient(
            [
                _FakeEventStreamResponse(
                    [
                        'data: {"id":"chatcmpl_kiro_tool","model":"claude-opus-4.7","choices":[{"delta":{"tool_calls":[{"index":0,"id":"tooluse_123","type":"function","function":{"name":"Read","arguments":""}}]},"finish_reason":null}]}',
                        'data: {"id":"chatcmpl_kiro_tool","model":"claude-opus-4.7","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"file_path\\":\\"foo.txt\\"}"}}]},"finish_reason":null}]}',
                        'data: {"id":"chatcmpl_kiro_tool","model":"claude-opus-4.7","choices":[{"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":9,"completion_tokens":4,"total_tokens":13}}',
                        "data: [DONE]",
                    ]
                ),
            ]
        )

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_stream_client", AsyncMock(return_value=stream_client)),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={
                        "authorization": "Bearer sk_test",
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": "claude-opus-4-7",
                        "stream": True,
                        "max_tokens": 64,
                        "messages": [{"role": "user", "content": "Read foo.txt"}],
                        "tools": [
                            {
                                "name": "Read",
                                "description": "Read a file",
                                "input_schema": {
                                    "type": "object",
                                    "properties": {"file_path": {"type": "string"}},
                                },
                            }
                        ],
                        "tool_choice": {"type": "tool", "name": "Read"},
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.text
        self.assertIn('"type":"tool_use"', body)
        self.assertIn('"name":"Read"', body)
        self.assertIn('"type":"input_json_delta"', body)
        self.assertIn('"stop_reason":"tool_use"', body)
        self.assertIn("event: message_stop", body)
        self.assertEqual(stream_client.calls[0]["json"]["tool_choice"], {"type": "tool", "name": "Read"})
        self.assertTrue(stream_client.calls[0]["json"]["stream"])

    async def test_messages_stream_kiro_go_passthrough_schedules_usage_write_after_stream(self):
        fake_user = SimpleNamespace(id="u_test", status="active", _api_key_id="k_kiro_stream_usage_task")
        settings.claude_compat_provider = "kiro_go"
        settings.claude_compat_base_url = "https://kiro-go.example"
        settings.claude_compat_api_key = "kiro-key"
        settings.claude_compat_auth_style = "bearer"
        registry._initialized = False
        registry.init_from_settings()
        stream_client = _RecordingStreamClient(
            [
                _FakeAnthropicEventStreamResponse(
                    [
                        'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_native_stream_task","type":"message","role":"assistant","model":"claude-opus-4.7","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":5,"cache_creation_input_tokens":8,"cache_read_input_tokens":7,"output_tokens":0}}}',
                        'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
                        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"OK"}}',
                        'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}',
                        'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":5,"cache_creation_input_tokens":8,"cache_read_input_tokens":7,"output_tokens":2}}',
                        'event: message_stop\ndata: {"type":"message_stop"}',
                    ]
                ),
            ]
        )

        real_create_task = asyncio.create_task
        scheduled = []

        def _capture_task(coro):
            task = real_create_task(coro)
            scheduled.append(task)
            return task

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_stream_client", AsyncMock(return_value=stream_client)),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()) as add_usage,
            patch.object(anthropic_module.asyncio, "create_task", side_effect=_capture_task),
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={
                        "authorization": "Bearer sk_test",
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": "claude-opus-4-7",
                        "stream": True,
                        "max_tokens": 64,
                        "messages": [{"role": "user", "content": "Reply with exactly OK"}],
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("event: message_stop", response.text)
        self.assertGreaterEqual(len(scheduled), 1)
        await asyncio.gather(*scheduled)
        add_usage.assert_awaited_once()

    async def test_messages_uses_station_alias_and_retail_price(self):
        fake_user = SimpleNamespace(
            id="u_station_child",
            status="active",
            _api_key_id="k_station_child",
            _station_context={
                "station_id": "st_1",
                "slug": "stone",
                "status": "active",
                "mode": "commission_station",
                "default_text_alias": "fast",
                "default_image_alias": "",
            },
        )
        station_model = SimpleNamespace(
            resolved_model=registry.resolve_public_model("gpt-5.4-mini", "chat/completions"),
            display_model="fast",
            station_id="st_1",
            station_alias="fast",
            resolved_public_model="gpt-5.4-mini",
            retail_input_per_million=120,
            retail_output_per_million=720,
            retail_price_per_image_cents=0.0,
            wholesale_input_per_million=75,
            wholesale_output_per_million=450,
            wholesale_price_per_image_cents=0.0,
            price_version=3,
        )
        client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "chatcmpl_station_alias",
                        "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                    }
                )
            ]
        )

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_http_client", AsyncMock(return_value=client)),
            patch.object(anthropic_module, "resolve_station_model_for_user", AsyncMock(return_value=station_model)),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()) as add_usage,
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={"authorization": "Bearer sk_station", "anthropic-version": "2023-06-01"},
                    json={
                        "model": "fast",
                        "max_tokens": 64,
                        "messages": [{"role": "user", "content": "Reply with exactly OK"}],
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["model"], "fast")
        self.assertEqual(client.calls[0]["json"]["model"], "gpt-5.4-mini")
        usage_kwargs = add_usage.await_args.kwargs
        self.assertEqual(usage_kwargs["model"], "fast")
        self.assertEqual(usage_kwargs["price_input_per_million"], 120)
        self.assertEqual(usage_kwargs["price_output_per_million"], 720)
        self.assertEqual(usage_kwargs["station_id"], "st_1")
        self.assertEqual(usage_kwargs["station_alias"], "fast")
        self.assertEqual(usage_kwargs["resolved_public_model"], "gpt-5.4-mini")
        self.assertEqual(usage_kwargs["price_version"], 3)

    async def test_messages_preserve_tool_roundtrip_semantics(self):
        fake_user = SimpleNamespace(id="u_test", status="active")
        client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "chatcmpl_tool_roundtrip",
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call_read_1",
                                            "type": "function",
                                            "function": {
                                                "name": "read_file",
                                                "arguments": "{\"path\":\"foo.txt\"}",
                                            },
                                        }
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                        "usage": {"prompt_tokens": 21, "completion_tokens": 7, "total_tokens": 28},
                    }
                )
            ]
        )

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_http_client", AsyncMock(return_value=client)),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={
                        "authorization": "Bearer sk_test",
                        "anthropic-version": "2023-06-01",
                        "user-agent": "claude-cli/2.1.121 (external, cli)",
                    },
                    json={
                        "model": "claude-opus-4-7",
                        "max_tokens": 256,
                        "tools": [
                            {
                                "name": "read_file",
                                "description": "Read a file",
                                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                            }
                        ],
                        "messages": [
                            {"role": "user", "content": [{"type": "text", "text": "Read foo.txt"}]},
                            {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": "call_read_1",
                                        "name": "read_file",
                                        "input": {"path": "foo.txt"},
                                    }
                                ],
                            },
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": "call_read_1",
                                        "content": [{"type": "text", "text": "file contents"}],
                                    }
                                ],
                            },
                        ],
                    },
                )

        self.assertEqual(response.status_code, 200)
        upstream_messages = client.calls[0]["json"]["messages"]
        self.assertEqual(upstream_messages[0], {"role": "user", "content": "Read foo.txt"})
        self.assertEqual(upstream_messages[1]["role"], "assistant")
        self.assertEqual(upstream_messages[1]["content"], None)
        self.assertEqual(upstream_messages[1]["tool_calls"][0]["function"]["name"], "read_file")
        self.assertEqual(upstream_messages[2], {"role": "tool", "tool_call_id": "call_read_1", "content": "file contents"})

        body = response.json()
        self.assertEqual(body["stop_reason"], "tool_use")
        self.assertEqual(body["content"][0]["type"], "tool_use")
        self.assertEqual(body["content"][0]["name"], "read_file")
        self.assertEqual(body["content"][0]["input"]["path"], "foo.txt")

    async def test_streaming_messages_translate_openai_sse_to_anthropic_sse(self):
        fake_user = SimpleNamespace(id="u_test", status="active", _api_key_id="k_claude_stream")
        stream_client = _RecordingStreamClient(
            [
                _FakeEventStreamResponse(
                    [
                        'data: {"id":"chatcmpl_stream","model":"gpt-5.5","choices":[{"delta":{"role":"assistant","content":"Hello"},"finish_reason":null}]}',
                        'data: {"id":"chatcmpl_stream","model":"gpt-5.5","choices":[{"delta":{"content":" world"},"finish_reason":"stop"}],"usage":{"prompt_tokens":12,"completion_tokens":3,"total_tokens":15,"prompt_tokens_details":{"cached_tokens":5}}}',
                        "data: [DONE]",
                    ]
                )
            ]
        )

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_stream_client", AsyncMock(return_value=stream_client)),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()) as add_usage,
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={
                        "authorization": "Bearer sk_test",
                        "anthropic-version": "2023-06-01",
                        "user-agent": "claude-cli/2.0.76 (external, cli)",
                    },
                    json={
                        "model": "claude-opus-4-7",
                        "max_tokens": 64,
                        "stream": True,
                        "messages": [{"role": "user", "content": "Reply with hello"}],
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: message_start", response.text)
        self.assertIn('"cache_creation_input_tokens":0', response.text)
        self.assertIn('"cache_read_input_tokens":0', response.text)
        self.assertIn("event: content_block_start", response.text)
        self.assertIn('"index":0', response.text)
        self.assertIn('"type":"text_delta"', response.text)
        self.assertIn("Hello", response.text)
        self.assertIn("event: message_stop", response.text)
        self.assertIn('"stop_reason":"end_turn"', response.text)
        self.assertIn('"cache_read_input_tokens":5', response.text)
        add_usage.assert_awaited_once()
        usage_kwargs = add_usage.await_args.kwargs
        self.assertEqual(usage_kwargs["api_key_id"], "k_claude_stream")
        self.assertEqual(usage_kwargs["cache_read_tokens"], 5)
        self.assertEqual(usage_kwargs["cache_creation_tokens"], 0)
        self.assertGreater(usage_kwargs["duration_ms"], 0)

    async def test_streaming_messages_returns_error_when_openai_upstream_stream_fails_before_events(self):
        fake_user = SimpleNamespace(id="u_test", status="active", _api_key_id="k_claude_stream_502")
        upstream = _FakeEventStreamResponse(
            [],
            status_code=502,
            headers={"content-type": "application/json"},
            body={"error": {"type": "server_error", "message": "upstream overloaded"}},
        )
        stream_client = _RecordingStreamClient([upstream])

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_stream_client", AsyncMock(return_value=stream_client)),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()) as add_usage,
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={
                        "authorization": "Bearer sk_test",
                        "anthropic-version": "2023-06-01",
                        "user-agent": "claude-cli/2.0.76 (external, cli)",
                    },
                    json={
                        "model": "claude-opus-4-7",
                        "max_tokens": 64,
                        "stream": True,
                        "messages": [{"role": "user", "content": "Reply with hello"}],
                    },
                )

        self.assertEqual(response.status_code, 502)
        body = response.json()
        self.assertEqual(body["type"], "error")
        self.assertEqual(body["error"]["type"], "server_error")
        self.assertEqual(body["error"]["message"], "upstream overloaded")
        self.assertNotIn("event: message_start", response.text)
        self.assertTrue(upstream._closed)
        add_usage.assert_not_awaited()

    async def test_streaming_tool_calls_translate_to_tool_use_events(self):
        fake_user = SimpleNamespace(id="u_test", status="active")
        stream_client = _RecordingStreamClient(
            [
                _FakeEventStreamResponse(
                    [
                        'data: {"id":"chatcmpl_tool","model":"gpt-5.5","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_123","type":"function","function":{"name":"read_file","arguments":""}}]},"finish_reason":null}]}',
                        'data: {"id":"chatcmpl_tool","model":"gpt-5.5","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"path\\":\\"foo.txt\\"}"}}]},"finish_reason":null}]}',
                        'data: {"id":"chatcmpl_tool","model":"gpt-5.5","choices":[{"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":9,"completion_tokens":4,"total_tokens":13}}',
                        "data: [DONE]",
                    ]
                )
            ]
        )

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_stream_client", AsyncMock(return_value=stream_client)),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={
                        "authorization": "Bearer sk_test",
                        "anthropic-version": "2023-06-01",
                        "user-agent": "claude-cli/2.0.76 (external, cli)",
                    },
                    json={
                        "model": "claude-opus-4-7",
                        "max_tokens": 64,
                        "stream": True,
                        "messages": [{"role": "user", "content": "Read foo.txt"}],
                        "tools": [{"name": "read_file", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}],
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertIn('"type":"tool_use"', response.text)
        self.assertIn('"name":"read_file"', response.text)
        self.assertIn('"type":"input_json_delta"', response.text)
        self.assertIn('"index":0', response.text)
        self.assertIn('\\"path\\":\\"foo.txt\\"', response.text)
        self.assertIn('"stop_reason":"tool_use"', response.text)

    async def test_streaming_reasoning_content_translates_to_thinking_and_signature_deltas(self):
        fake_user = SimpleNamespace(id="u_test", status="active")
        stream_client = _RecordingStreamClient(
            [
                _FakeEventStreamResponse(
                    [
                        'data: {"id":"chatcmpl_reasoning","model":"gpt-5.5","choices":[{"delta":{"reasoning_content":[{"type":"reasoning","text":"Thinking step 1. ","signature":"sig_abc"},{"type":"reasoning","text":"Thinking step 2."}]},"finish_reason":null}]}',
                        'data: {"id":"chatcmpl_reasoning","model":"gpt-5.5","choices":[{"delta":{"content":"Final answer"},"finish_reason":"stop"}],"usage":{"prompt_tokens":14,"completion_tokens":6,"total_tokens":20}}',
                        "data: [DONE]",
                    ]
                )
            ]
        )

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_stream_client", AsyncMock(return_value=stream_client)),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={
                        "authorization": "Bearer sk_test",
                        "anthropic-version": "2023-06-01",
                        "user-agent": "claude-cli/2.1.121 (external, cli)",
                    },
                    json={
                        "model": "claude-opus-4-7",
                        "max_tokens": 128,
                        "stream": True,
                        "messages": [{"role": "user", "content": "Reason step by step"}],
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertIn('"type":"thinking_delta"', response.text)
        self.assertIn('Thinking step 1.', response.text)
        self.assertIn('Thinking step 2.', response.text)
        self.assertIn('"type":"signature_delta"', response.text)
        self.assertIn('"signature":"sig_abc"', response.text)
        self.assertIn('Final answer', response.text)
        self.assertIn('"stop_reason":"end_turn"', response.text)

    async def test_streaming_reasoning_block_stops_before_text_block_starts(self):
        fake_user = SimpleNamespace(id="u_test", status="active")
        stream_client = _RecordingStreamClient(
            [
                _FakeEventStreamResponse(
                    [
                        'data: {"id":"chatcmpl_reasoning_order","model":"gpt-5.5","choices":[{"delta":{"reasoning_content":[{"type":"reasoning","text":"Thinking step. ","signature":"sig_order"}]},"finish_reason":null}]}',
                        'data: {"id":"chatcmpl_reasoning_order","model":"gpt-5.5","choices":[{"delta":{"content":"Question for user?"},"finish_reason":"stop"}],"usage":{"prompt_tokens":18,"completion_tokens":5,"total_tokens":23}}',
                        "data: [DONE]",
                    ]
                )
            ]
        )

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_stream_client", AsyncMock(return_value=stream_client)),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={
                        "authorization": "Bearer sk_test",
                        "anthropic-version": "2023-06-01",
                        "user-agent": "claude-cli/2.1.121 (external, cli)",
                    },
                    json={
                        "model": "claude-opus-4-7",
                        "max_tokens": 128,
                        "stream": True,
                        "messages": [{"role": "user", "content": "Research first, then ask me a question"}],
                    },
                )

        self.assertEqual(response.status_code, 200)
        body = response.text
        thinking_start = body.find('"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":"","signature":""}')
        signature_delta = body.find('"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"sig_order"}}')
        thinking_stop = body.find('"type":"content_block_stop","index":0}')
        text_start = body.find('"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}')
        text_delta = body.find('"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"Question for user?"}}')

        self.assertGreaterEqual(thinking_start, 0)
        self.assertGreater(signature_delta, thinking_start)
        self.assertGreater(thinking_stop, signature_delta)
        self.assertGreater(text_start, thinking_stop)
        self.assertGreater(text_delta, text_start)

    async def test_streaming_reasoning_block_stops_before_tool_block_starts(self):
        fake_user = SimpleNamespace(id="u_test", status="active")
        stream_client = _RecordingStreamClient(
            [
                _FakeEventStreamResponse(
                    [
                        'data: {"id":"chatcmpl_reasoning_tool","model":"gpt-5.5","choices":[{"delta":{"reasoning_content":[{"type":"reasoning","text":"Need a tool. ","signature":"sig_tool"}]},"finish_reason":null}]}',
                        'data: {"id":"chatcmpl_reasoning_tool","model":"gpt-5.5","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_789","type":"function","function":{"name":"ask_user","arguments":""}}]},"finish_reason":null}]}',
                        'data: {"id":"chatcmpl_reasoning_tool","model":"gpt-5.5","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"question\\":\\"Proceed?\\"}"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":20,"completion_tokens":7,"total_tokens":27}}',
                        "data: [DONE]",
                    ]
                )
            ]
        )

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_stream_client", AsyncMock(return_value=stream_client)),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={
                        "authorization": "Bearer sk_test",
                        "anthropic-version": "2023-06-01",
                        "user-agent": "claude-cli/2.1.121 (external, cli)",
                    },
                    json={
                        "model": "claude-opus-4-7",
                        "max_tokens": 128,
                        "stream": True,
                        "messages": [{"role": "user", "content": "Think, then ask for confirmation"}],
                        "tools": [{"name": "ask_user", "input_schema": {"type": "object", "properties": {"question": {"type": "string"}}}}],
                    },
                )

        self.assertEqual(response.status_code, 200)
        body = response.text
        thinking_stop = body.find('"type":"content_block_stop","index":0}')
        tool_start = body.find('"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"call_789","name":"ask_user","input":{}}}')
        tool_delta = body.find('"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"question\\":\\"Proceed?\\"}"}}')

        self.assertGreaterEqual(thinking_stop, 0)
        self.assertGreater(tool_start, thinking_stop)
        self.assertGreater(tool_delta, tool_start)
        self.assertIn('"stop_reason":"tool_use"', body)

    async def test_tool_result_non_text_blocks_are_preserved_in_tool_message(self):
        fake_user = SimpleNamespace(id="u_test", status="active")
        client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "chatcmpl_tool_result_shape",
                        "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                        "usage": {"prompt_tokens": 11, "completion_tokens": 1, "total_tokens": 12},
                    }
                )
            ]
        )

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_http_client", AsyncMock(return_value=client)),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={
                        "authorization": "Bearer sk_test",
                        "anthropic-version": "2023-06-01",
                        "user-agent": "claude-cli/2.1.121 (external, cli)",
                    },
                    json={
                        "model": "claude-opus-4-7",
                        "max_tokens": 64,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": "toolu_123",
                                        "content": [
                                            {"type": "tool_reference", "tool_name": "mcp__nia__manage_resource"},
                                            {"type": "text", "text": "plain result"},
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                )

        self.assertEqual(response.status_code, 200)
        upstream_messages = client.calls[0]["json"]["messages"]
        tool_messages = [msg for msg in upstream_messages if msg.get("role") == "tool"]
        self.assertTrue(tool_messages)
        self.assertEqual(tool_messages[0]["tool_call_id"], "toolu_123")
        self.assertIn('"type":"tool_reference"', tool_messages[0]["content"])
        self.assertIn('plain result', tool_messages[0]["content"])

    async def test_tool_result_object_content_is_serialized_for_follow_up_turns(self):
        fake_user = SimpleNamespace(id="u_test", status="active")
        client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "chatcmpl_tool_result_object",
                        "choices": [{"message": {"role": "assistant", "content": "continue"}}],
                        "usage": {"prompt_tokens": 17, "completion_tokens": 2, "total_tokens": 19},
                    }
                )
            ]
        )

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_http_client", AsyncMock(return_value=client)),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={
                        "authorization": "Bearer sk_test",
                        "anthropic-version": "2023-06-01",
                        "user-agent": "claude-cli/2.1.121 (external, cli)",
                    },
                    json={
                        "model": "claude-opus-4-7",
                        "max_tokens": 64,
                        "messages": [
                            {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": "toolu_ask_1",
                                        "name": "AskUserQuestion",
                                        "input": {
                                            "questions": [
                                                {
                                                    "header": "Scope",
                                                    "question": "Which direction?",
                                                    "options": [
                                                        {"label": "A", "description": "Option A"},
                                                        {"label": "B", "description": "Option B"},
                                                    ],
                                                }
                                            ]
                                        },
                                    }
                                ],
                            },
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": "toolu_ask_1",
                                        "content": {
                                            "questions": [
                                                {
                                                    "header": "Scope",
                                                    "question": "Which direction?",
                                                    "answer": "A",
                                                }
                                            ]
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                )

        self.assertEqual(response.status_code, 200)
        upstream_messages = client.calls[0]["json"]["messages"]
        tool_messages = [msg for msg in upstream_messages if msg.get("role") == "tool"]
        self.assertTrue(tool_messages)
        self.assertEqual(tool_messages[0]["tool_call_id"], "toolu_ask_1")
        self.assertIn('"questions":[{"header":"Scope","question":"Which direction?","answer":"A"}]', tool_messages[0]["content"])

    async def test_assistant_thinking_only_history_is_dropped_in_follow_up_turns(self):
        fake_user = SimpleNamespace(id="u_test", status="active")
        client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "chatcmpl_thinking_only",
                        "choices": [{"message": {"role": "assistant", "content": "continue"}}],
                        "usage": {"prompt_tokens": 15, "completion_tokens": 2, "total_tokens": 17},
                    }
                )
            ]
        )

        with (
            patch.object(anthropic_module, "authorize_request", AsyncMock(return_value=fake_user)),
            patch.object(anthropic_module, "get_http_client", AsyncMock(return_value=client)),
            patch.object(anthropic_module.usage_buffer, "add", AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as http_client:
                response = await http_client.post(
                    "/v1/messages",
                    headers={
                        "authorization": "Bearer sk_test",
                        "anthropic-version": "2023-06-01",
                        "user-agent": "claude-cli/2.1.121 (external, cli)",
                    },
                    json={
                        "model": "claude-opus-4-7",
                        "max_tokens": 64,
                        "messages": [
                            {
                                "role": "assistant",
                                "content": [
                                    {"type": "thinking", "thinking": "internal only", "signature": "sig_valid"}
                                ],
                            },
                            {"role": "user", "content": [{"type": "text", "text": "继续"}]},
                        ],
                    },
                )

        self.assertEqual(response.status_code, 200)
        upstream_messages = client.calls[0]["json"]["messages"]
        assistant_messages = [msg for msg in upstream_messages if msg.get("role") == "assistant"]
        self.assertFalse(assistant_messages)
        self.assertIn({"role": "user", "content": "继续"}, upstream_messages)
