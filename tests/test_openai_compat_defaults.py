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


class _FakeUpstreamResponse:
    def __init__(self, payload: dict, status_code: int = 200, content_type: str = "application/json") -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = {"content-type": content_type}

    def json(self):
        return self._payload

    @property
    def text(self) -> str:
        return json.dumps(self._payload, ensure_ascii=False)


class _RecordingClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if not self.responses:
            raise AssertionError("unexpected upstream call")
        return self.responses.pop(0)


class OpenAICompatDefaultsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._originals = {
            "fixed_model": settings.fixed_model,
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
            "gateway_auth_style": settings.gateway_auth_style,
            "model_catalog_json": settings.model_catalog_json,
        }

        settings.fixed_model = "gpt-5.2-codex"
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
        settings.fallback_model = "gpt-5.2-codex"
        settings.fallback_upstream_url = "https://fallback.example/v1"
        settings.fallback_api_key = "fallback-key"
        settings.fallback_price_input = 99
        settings.fallback_price_output = 699
        settings.fallback_auth_style = "azure"
        settings.gateway_auth_style = "bearer"
        settings.model_catalog_json = json.dumps(
            {
                "default_text_model": "gpt-5.2-codex",
                "default_image_model": "gemini-image",
                "models": [
                    {
                        "id": "gpt-5.2-codex",
                        "owned_by": "openai",
                        "provider_name": "OpenAI",
                        "capabilities": ["chat/completions", "responses", "embeddings"],
                        "routing_mode": "legacy_auto",
                    },
                    {
                        "id": "gemini-fast",
                        "owned_by": "google",
                        "provider_name": "Google",
                        "provider_model": "gemini-2.5-flash",
                        "capabilities": ["chat/completions", "responses"],
                        "routing_mode": "direct",
                        "upstream_model": "gemini-fast",
                        "upstream_url": "https://gateway.example/v1",
                        "api_key": "gateway-key",
                        "auth_style": "bearer",
                    },
                    {
                        "id": "gemini-image",
                        "owned_by": "google",
                        "provider_name": "Google",
                        "provider_model": "gemini-2.5-flash-image",
                        "capabilities": ["images/generations"],
                        "routing_mode": "direct",
                        "upstream_model": "vertex-gemini-2.5-flash-image",
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
        self.assertEqual(payload["model"], "gpt-5.2-codex")
        self.assertEqual(payload["choices"][0]["message"]["content"], "OK")
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(upstream_client.calls[0]["url"], "https://legacy.example/v1/responses")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "gpt-4o-mini")
        self.assertEqual(upstream_client.calls[0]["headers"]["api-key"], "legacy-key")

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
        self.assertEqual(payload["model"], "gpt-5.2-codex")
        self.assertEqual(payload["output"][0]["content"][0]["text"], "OK")
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(upstream_client.calls[0]["url"], "https://legacy.example/v1/responses")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "gpt-4o-mini")
        self.assertEqual(upstream_client.calls[0]["headers"]["api-key"], "legacy-key")

    async def test_image_without_model_uses_default_image_alias(self) -> None:
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {"created": 1774380000, "data": [{"b64_json": "abc"}]}
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
        self.assertEqual(upstream_client.calls[0]["url"], "https://gateway.example/v1/images/generations")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "vertex-gemini-2.5-flash-image")
        self.assertEqual(upstream_client.calls[0]["headers"]["authorization"], "Bearer gateway-key")

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

    async def test_models_endpoint_returns_curated_metadata(self) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/v1/models")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["object"], "list")
        self.assertEqual(
            [item["id"] for item in payload["data"]],
            ["gpt-5.2-codex", "gemini-fast", "gemini-image"],
        )
        self.assertEqual(payload["data"][0]["coincoin_provider"], "OpenAI")
        self.assertEqual(payload["data"][0]["coincoin_billable_sku"], "gpt-5.2-codex")
        self.assertEqual(payload["data"][0]["coincoin_default_for"], ["text"])
        self.assertEqual(payload["data"][1]["coincoin_provider"], "Google")
        self.assertEqual(payload["data"][1]["coincoin_provider_model"], "gemini-2.5-flash")
        self.assertEqual(payload["data"][1]["coincoin_capabilities"], ["chat/completions", "responses"])
        self.assertEqual(payload["data"][1]["coincoin_billable_sku"], "gemini-fast")
        self.assertEqual(payload["data"][2]["coincoin_capabilities"], ["images/generations"])
        self.assertEqual(payload["data"][2]["coincoin_default_for"], ["image"])

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
