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
        self.assertEqual(client.calls[0]["url"], "https://cliproxyapi-deploy-production.up.railway.app/v1/messages")
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
