import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("COINCOIN_DATABASE_URL", "mysql://test:test@127.0.0.1:3306/test")

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app.anthropic_compat as anthropic_module
import app.openai_compat as openai_module
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


class _FakeEventStreamResponse:
    def __init__(self, lines, status_code=200, headers=None):
        self._lines = list(lines)
        self.status_code = status_code
        self.headers = {"content-type": "text/event-stream", **(headers or {})}
        self._closed = False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aclose(self):
        self._closed = True


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
        settings.upstream_base_url = "https://cliproxyapi-deploy-production.up.railway.app/v1"
        settings.upstream_api_key = "sk-cliproxy-test"
        settings.fixed_model = "gpt-5.5"
        settings.primary_auth_style = "bearer"
        settings.router_enabled = False
        registry.init_from_settings()
        self.app = FastAPI()
        self.app.include_router(anthropic_module.router)
        self.app.include_router(openai_module.router)

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
        fake_user = SimpleNamespace(id="u_test", status="active")
        client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "chatcmpl_test",
                        "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
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
        self.assertEqual(client.calls[0]["url"], "https://cliproxyapi-deploy-production.up.railway.app/v1/chat/completions")
        self.assertEqual(client.calls[0]["json"]["model"], "gpt-5.5")

    async def test_claude_alias_resolves_to_gpt_55_upstream(self):
        fake_user = SimpleNamespace(id="u_test", status="active")
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
        fake_user = SimpleNamespace(id="u_test", status="active")
        stream_client = _RecordingStreamClient(
            [
                _FakeEventStreamResponse(
                    [
                        'data: {"id":"chatcmpl_stream","model":"gpt-5.5","choices":[{"delta":{"role":"assistant","content":"Hello"},"finish_reason":null}]}',
                        'data: {"id":"chatcmpl_stream","model":"gpt-5.5","choices":[{"delta":{"content":" world"},"finish_reason":"stop"}],"usage":{"prompt_tokens":12,"completion_tokens":3,"total_tokens":15}}',
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
        self.assertIn("event: content_block_start", response.text)
        self.assertIn('"index":0', response.text)
        self.assertIn('"type":"text_delta"', response.text)
        self.assertIn("Hello", response.text)
        self.assertIn("event: message_stop", response.text)
        self.assertIn('"stop_reason":"end_turn"', response.text)
        add_usage.assert_awaited_once()

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
