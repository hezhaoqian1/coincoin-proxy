import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.channel_router import ModelChannelRouteSnapshot, ProviderChannelSnapshot, channel_router
from app.config import settings
from app.fallback_alerts import FallbackExhaustedAlert
from app.main import app
from app.router import registry
import app.fallback_alerts as fallback_alerts
import app.proxy as proxy_module
import app.openai_compat as openai_module


LEGACY_PUBLIC_TEXT_MODELS = [
    "gpt-5.4",
    "gpt-5",
    "gpt-5.5",
    "gpt-5.1",
    "gpt-5.1-codex",
    "gpt-5.1-codex-mini",
    "gpt-5.1-codex-max",
    "gpt-5.2",
    "gpt-5.2-codex",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
    "codex-auto-review",
    "gpt-5.4-mini",
    "gpt-5-codex",
    "gpt-5-codex-mini",
]
LEGACY_PUBLIC_TEXT_PRICES = {
    "gpt-5.4": (250, 1500),
    "gpt-5": (125, 1000),
    "gpt-5.5": (500, 3000),
    "gpt-5.1": (125, 1000),
    "gpt-5.1-codex": (125, 1000),
    "gpt-5.1-codex-mini": (75, 450),
    "gpt-5.1-codex-max": (500, 3000),
    "gpt-5.2": (175, 1400),
    "gpt-5.2-codex": (175, 1400),
    "gpt-5.3-codex": (175, 1400),
    "gpt-5.3-codex-spark": (175, 1400),
    "codex-auto-review": (500, 3000),
    "gpt-5.4-mini": (75, 450),
    "gpt-5-codex": (175, 1400),
    "gpt-5-codex-mini": (75, 450),
}


def _legacy_text_model(model_id: str) -> dict:
    provider_model = "gpt-5.3-codex" if model_id == "gpt-5.2-codex" else model_id
    model = {
        "id": model_id,
        "owned_by": "openai",
        "provider_name": "OpenAI",
        "provider_model": provider_model,
        "capabilities": ["chat/completions", "responses"],
        "routing_mode": "legacy_auto",
        "delivery_lane": "legacy",
    }
    prices = LEGACY_PUBLIC_TEXT_PRICES.get(model_id)
    if prices:
        model["price_input_per_million"] = prices[0]
        model["price_output_per_million"] = prices[1]
    if model_id == "gpt-5.4":
        model["metadata"] = {
            "execution_profile": "legacy_general",
            "execution_pool": "cpa_general_pool",
            "legacy_default_slot": "cheap",
            "honor_tool_routing": True,
        }
    elif model_id in {"gpt-5.2-codex", "gpt-5.3-codex", "gpt-5.3-codex-spark", "codex-auto-review"}:
        model["metadata"] = {
            "execution_profile": "legacy_coding",
            "execution_pool": "cpa_coding_pool",
            "legacy_default_slot": "premium",
            "honor_tool_routing": False,
        }
        if model_id == "gpt-5.3-codex-spark":
            model["created"] = 1770912000
            model["metadata"].update(
                {
                    "display_name": "GPT 5.3 Codex Spark",
                    "version": "gpt-5.3",
                    "description": "Ultra-fast coding model.",
                    "context_length": 128000,
                    "max_completion_tokens": 128000,
                    "supported_parameters": ["tools"],
                    "thinking": {"levels": ["low", "medium", "high", "xhigh"]},
                }
            )
        if model_id == "codex-auto-review":
            model["created"] = 1776902400
            model["metadata"].update(
                {
                    "display_name": "Codex Auto Review",
                    "version": "Codex Auto Review",
                    "description": "Automatic approval review model for Codex.",
                    "context_length": 272000,
                    "max_completion_tokens": 128000,
                    "supported_parameters": ["tools"],
                    "thinking": {"levels": ["low", "medium", "high", "xhigh"]},
                }
            )
    return model


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

    async def aiter_bytes(self):
        yield self.text.encode("utf-8")

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
            "cache_discount_rate": settings.cache_discount_rate,
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
            "gemini_cpa_auth_style": settings.gemini_cpa_auth_style,
            "image_edit_sync_gateway_timeout_seconds": settings.image_edit_sync_gateway_timeout_seconds,
            "vertex_api_key": settings.vertex_api_key,
            "vertex_gemini_api_base": settings.vertex_gemini_api_base,
            "claude_compat_provider": settings.claude_compat_provider,
            "claude_compat_base_url": settings.claude_compat_base_url,
            "claude_compat_api_key": settings.claude_compat_api_key,
            "claude_compat_auth_style": settings.claude_compat_auth_style,
            "model_catalog_json": settings.model_catalog_json,
            "fallback_alert_webhook_url": settings.fallback_alert_webhook_url,
            "fallback_alert_keyword": settings.fallback_alert_keyword,
            "fallback_alert_dedup_seconds": settings.fallback_alert_dedup_seconds,
        }

        settings.fixed_model = "gpt-5.4"
        settings.embedding_model = "text-embedding-3-small"
        settings.embedding_upstream_url = ""
        settings.embedding_api_key = ""
        settings.embedding_auth_style = ""
        settings.embedding_price_input = 2
        settings.router_enabled = True
        settings.upstream_base_url = "https://legacy.example/v1"
        settings.upstream_api_key = "legacy-key"
        settings.price_input_per_million = 500
        settings.price_output_per_million = 3000
        settings.cache_discount_rate = 0.1
        settings.primary_auth_style = "azure"
        settings.primary_strip_unsupported = False
        settings.cheap_model = "gpt-4o-mini"
        settings.cheap_upstream_url = "https://legacy.example/v1"
        settings.cheap_api_key = "legacy-key"
        settings.cheap_price_input = 75
        settings.cheap_price_output = 450
        settings.fallback_model = "gpt-5.4"
        settings.fallback_upstream_url = "https://fallback.example/v1"
        settings.fallback_api_key = "fallback-key"
        settings.fallback_price_input = 500
        settings.fallback_price_output = 3000
        settings.fallback_auth_style = "azure"
        settings.gateway_base_url = ""
        settings.gateway_api_key = ""
        settings.gateway_auth_style = "bearer"
        settings.gemini_cpa_auth_style = "bearer"
        settings.image_edit_sync_gateway_timeout_seconds = 60
        settings.vertex_api_key = ""
        settings.vertex_gemini_api_base = "https://aiplatform.googleapis.com/v1/publishers/google"
        settings.claude_compat_provider = "upstream_direct"
        settings.claude_compat_base_url = "https://kiro-go.example"
        settings.claude_compat_api_key = "kiro-key"
        settings.claude_compat_auth_style = "bearer"
        settings.fallback_alert_webhook_url = ""
        settings.fallback_alert_keyword = ""
        settings.fallback_alert_dedup_seconds = 900
        settings.model_catalog_json = json.dumps(
            {
                "default_text_model": "gpt-5.4",
                "default_embedding_model": "text-embedding-3-small",
                "default_image_model": "gpt-image-2",
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
                        "price_input_per_million": 2,
                        "price_output_per_million": 0,
                        "billable_sku": "azure-text-embedding-3-small",
                    },
                    {
                        "id": "gpt-image-2",
                        "owned_by": "openai",
                        "provider_name": "OpenAI",
                        "provider_model": "gpt-image-2",
                        "capabilities": ["images/generations", "images/edits"],
                        "routing_mode": "direct",
                        "delivery_lane": "upstream_direct",
                        "upstream_model": "gpt-image-2",
                        "upstream_url": "https://cliproxy.example/v1",
                        "api_key": "cliproxy-key",
                        "auth_style": "bearer",
                        "price_per_image_cents": 5.3,
                        "billable_sku": "openai-image",
                    },
                    {
                        "id": "gemini-fast",
                        "owned_by": "google",
                        "provider_name": "Google",
                        "provider_model": "gemini-2.5-flash",
                        "capabilities": ["chat/completions", "responses"],
                        "routing_mode": "direct",
                        "delivery_lane": "cpa_gemini",
                        "upstream_model": "gemini-2.5-flash",
                        "upstream_url": "https://gemini-cpa.example/v1",
                        "api_key": "gemini-cpa-key",
                        "auth_style": "bearer",
                        "metadata": {"provider_platform": "cpa_gemini"},
                    },
                    {
                        "id": "gemini-image",
                        "owned_by": "google",
                        "provider_name": "Google",
                        "provider_model": "gemini-3.1-flash-image",
                        "capabilities": ["images/generations", "images/edits"],
                        "routing_mode": "direct",
                        "delivery_lane": "cpa_gemini",
                        "upstream_model": "gemini-3.1-flash-image",
                        "upstream_url": "https://gemini-cpa.example/v1",
                        "api_key": "gemini-cpa-key",
                        "auth_style": "bearer",
                        "metadata": {"provider_platform": "cpa_gemini"},
                    },
                ],
            }
        )
        registry._initialized = False
        registry.init_from_settings()
        channel_router.clear_snapshot()
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

    def _set_gemini_image_gateway_lane(self) -> None:
        catalog = json.loads(settings.model_catalog_json)
        for model in catalog.get("models") or []:
            if model.get("id") == "gemini-image":
                model["delivery_lane"] = "gateway"
                model["upstream_model"] = "gemini-image"
                model["upstream_url"] = "https://gateway.example/v1"
                model["api_key"] = "gateway-key"
                model["auth_style"] = "bearer"
        settings.model_catalog_json = json.dumps(catalog)
        registry._initialized = False
        registry.init_from_settings()

    def _add_claude_code_model(self) -> None:
        catalog = json.loads(settings.model_catalog_json)
        catalog.setdefault("models", []).append(
            {
                "id": "claude-opus-4-7",
                "owned_by": "coincoin",
                "provider_name": "OpenAI",
                "provider_model": "gpt-5.5",
                "capabilities": ["chat/completions", "responses"],
                "routing_mode": "direct",
                "delivery_lane": "upstream_direct",
                "upstream_model": "gpt-5.5",
                "upstream_url": "https://legacy.example/v1",
                "api_key": "legacy-key",
                "auth_style": "bearer",
                "price_input_per_million": 500,
                "price_output_per_million": 3000,
                "billable_sku": "claude-code-compat-text",
                "metadata": {"compat_family": "claude-code"},
            }
        )
        settings.model_catalog_json = json.dumps(catalog)
        registry._initialized = False
        registry.init_from_settings()

    def _add_root_base_text_model(self) -> None:
        catalog = json.loads(settings.model_catalog_json)
        catalog.setdefault("models", []).append(
            {
                "id": "root-base-model",
                "owned_by": "coincoin",
                "provider_name": "OpenAI Compatible",
                "provider_model": "root-upstream-model",
                "capabilities": ["chat/completions", "responses"],
                "routing_mode": "direct",
                "delivery_lane": "upstream_direct",
                "upstream_model": "root-upstream-model",
                "upstream_url": "https://root-base.example",
                "api_key": "root-key",
                "auth_style": "bearer",
                "price_input_per_million": 100,
                "price_output_per_million": 200,
            }
        )
        settings.model_catalog_json = json.dumps(catalog)
        registry._initialized = False
        registry.init_from_settings()

    def tearDown(self) -> None:
        for key, value in self._originals.items():
            setattr(settings, key, value)
        channel_router.clear_snapshot()
        fallback_alerts.reset_fallback_alert_state()
        registry._initialized = False
        app.dependency_overrides.pop(proxy_module.get_db, None)
        app.dependency_overrides.pop(openai_module.get_db, None)

    async def test_responses_normalizes_root_base_url_for_openai_compatible_upstream(self) -> None:
        self._add_root_base_text_model()
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "resp_root_base",
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
            ), patch.object(proxy_module.usage_buffer, "add", AsyncMock()):
                response = await client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={"model": "root-base-model", "input": "Reply with only: OK"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(upstream_client.calls[0]["url"], "https://root-base.example/v1/responses")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "root-upstream-model")
        self.assertEqual(upstream_client.calls[0]["headers"]["authorization"], "Bearer root-key")

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

    async def test_chat_normalizes_root_base_url_for_responses_upstream(self) -> None:
        self._add_root_base_text_model()
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "resp_chat_root_base",
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
            ), patch.object(openai_module.usage_buffer, "add", AsyncMock()):
                response = await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={
                        "model": "root-base-model",
                        "messages": [{"role": "user", "content": "Reply with only: OK"}],
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["choices"][0]["message"]["content"], "OK")
        self.assertEqual(upstream_client.calls[0]["url"], "https://root-base.example/v1/responses")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "root-upstream-model")
        self.assertEqual(upstream_client.calls[0]["headers"]["authorization"], "Bearer root-key")

    async def test_fallback_alert_deduplicates_same_failure(self) -> None:
        settings.fallback_alert_webhook_url = "https://dingtalk.example/robot"
        settings.fallback_alert_keyword = "CoinCoinAlert"
        settings.fallback_alert_dedup_seconds = 900
        alert = FallbackExhaustedAlert(
            endpoint="responses",
            model="gpt-5.3-codex",
            status_code=503,
            reason="upstream_unreachable",
            route_reason="system_fallback:500",
            channel_id="system:legacy_cpa",
            fallback_from_channel_id="ch_primary",
            route_attempt=1,
        )

        def fake_create_task(coro):
            coro.close()
            return object()

        with patch.object(fallback_alerts.asyncio, "create_task", side_effect=fake_create_task) as create_task:
            self.assertTrue(fallback_alerts.notify_fallback_exhausted(alert))
            self.assertFalse(fallback_alerts.notify_fallback_exhausted(alert))

        create_task.assert_called_once()
        payload = fallback_alerts.build_dingtalk_text_payload(alert)
        self.assertTrue(payload["text"]["content"].startswith("CoinCoinAlert CoinCoin fallback 全部失败"))

    async def test_chat_empty_nonstream_json_collapses_stream_output(self) -> None:
        settings.router_enabled = False
        settings.primary_auth_style = "bearer"
        registry._initialized = False
        registry.init_from_settings()
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "resp_empty_chat",
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
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
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "gpt-5.3-codex")
        self.assertEqual(upstream_client.calls[0]["url"], "https://legacy.example/v1/responses")

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
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "gpt-5.4-mini")
        self.assertEqual(upstream_client.calls[0]["url"], "https://legacy.example/v1/responses")

    async def test_responses_explicit_gpt_5_3_codex_spark_alias_keeps_public_model_name(self) -> None:
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "resp_legacy_spark_alias",
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
                    json={"model": "gpt-5.3-codex-spark", "input": "Reply with only: OK"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["model"], "gpt-5.3-codex-spark")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "gpt-5.3-codex-spark")
        self.assertEqual(upstream_client.calls[0]["url"], "https://legacy.example/v1/responses")

    async def test_responses_explicit_legacy_alias_does_not_fallback_to_a_different_model(self) -> None:
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {"error": {"message": "primary failed", "type": "server_error"}},
                    status_code=500,
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
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={"model": "gpt-5.2-codex", "input": "Reply with only: OK"},
                )

        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "gpt-5.3-codex")
        self.assertEqual(upstream_client.calls[0]["url"], "https://legacy.example/v1/responses")

    async def test_responses_provider_channel_falls_back_to_next_channel_on_retryable_status(self) -> None:
        channel_router.set_snapshot(
            [
                ProviderChannelSnapshot(
                    channel_id="ch_test_primary",
                    provider_platform="sub2api",
                    base_url="https://primary-channel.example/v1",
                    api_key="primary-key",
                    auth_style="bearer",
                    priority=0,
                    allowed_fails=1,
                    cooldown_seconds=30,
                ),
                ProviderChannelSnapshot(
                    channel_id="ch_test_backup",
                    provider_platform="new_api",
                    base_url="https://backup-channel.example/v1",
                    api_key="backup-key",
                    auth_style="bearer",
                    priority=10,
                ),
            ],
            [
                ModelChannelRouteSnapshot(
                    route_id="mcr_test_primary",
                    public_model_id="gpt-5.3-codex",
                    endpoint="responses",
                    channel_id="ch_test_primary",
                ),
                ModelChannelRouteSnapshot(
                    route_id="mcr_test_backup",
                    public_model_id="gpt-5.3-codex",
                    endpoint="responses",
                    channel_id="ch_test_backup",
                ),
            ],
        )
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {"error": {"message": "primary failed", "type": "server_error"}},
                    status_code=500,
                ),
                _FakeUpstreamResponse(
                    {
                        "id": "resp_channel_fallback",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": "OK"}],
                            }
                        ],
                        "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
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
            ), patch.object(proxy_module.usage_buffer, "add", AsyncMock()) as add_usage:
                response = await client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={"model": "gpt-5.3-codex", "input": "Reply with only: OK"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(upstream_client.calls), 2)
        self.assertEqual(upstream_client.calls[0]["url"], "https://primary-channel.example/v1/responses")
        self.assertEqual(upstream_client.calls[0]["headers"]["authorization"], "Bearer primary-key")
        self.assertEqual(upstream_client.calls[1]["url"], "https://backup-channel.example/v1/responses")
        self.assertEqual(upstream_client.calls[1]["headers"]["authorization"], "Bearer backup-key")
        add_usage.assert_awaited_once()
        usage_kwargs = add_usage.await_args.kwargs
        self.assertEqual(usage_kwargs["route_reason"], "channel_fallback:500")
        self.assertEqual(usage_kwargs["channel_id"], "ch_test_backup")
        self.assertEqual(usage_kwargs["fallback_from_channel_id"], "ch_test_primary")
        self.assertEqual(usage_kwargs["route_attempt"], 1)

    async def test_responses_alerts_once_when_provider_and_system_fallbacks_fail(self) -> None:
        channel_router.set_snapshot(
            [
                ProviderChannelSnapshot(
                    channel_id="ch_test_primary",
                    provider_platform="sub2api",
                    base_url="https://primary-channel.example/v1",
                    api_key="primary-key",
                    auth_style="bearer",
                    priority=0,
                    allowed_fails=99,
                    cooldown_seconds=30,
                ),
            ],
            [
                ModelChannelRouteSnapshot(
                    route_id="mcr_test_primary",
                    public_model_id="gpt-5.3-codex",
                    endpoint="responses",
                    channel_id="ch_test_primary",
                ),
            ],
        )
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {"error": {"message": "provider failed", "type": "server_error", "code": "provider_failed"}},
                    status_code=500,
                    headers={"x-request-id": "req_provider"},
                ),
                _FakeUpstreamResponse(
                    {"error": {"message": "system fallback failed", "type": "server_error", "code": "system_failed"}},
                    status_code=503,
                    headers={"x-request-id": "req_system"},
                ),
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                proxy_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ), patch.object(proxy_module, "notify_fallback_exhausted", return_value=True) as notify:
                response = await client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={"model": "gpt-5.3-codex", "input": "Reply with only: OK"},
                )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(len(upstream_client.calls), 2)
        notify.assert_called_once()
        alert = notify.call_args.args[0]
        self.assertIsInstance(alert, FallbackExhaustedAlert)
        self.assertEqual(alert.endpoint, "responses")
        self.assertEqual(alert.model, "gpt-5.3-codex")
        self.assertEqual(alert.status_code, 503)
        self.assertEqual(alert.reason, "system_failed")
        self.assertEqual(alert.route_reason, "system_fallback:500")
        self.assertEqual(alert.channel_id, "system:legacy_cpa")
        self.assertEqual(alert.fallback_from_channel_id, "ch_test_primary")
        self.assertEqual(alert.route_attempt, 1)
        self.assertEqual(alert.upstream_request_id, "req_system")

    async def test_responses_provider_channel_falls_back_to_system_catalog_when_no_peer_route(self) -> None:
        channel_router.set_snapshot(
            [
                ProviderChannelSnapshot(
                    channel_id="ch_test_primary",
                    provider_platform="sub2api",
                    base_url="https://primary-channel.example/v1",
                    api_key="primary-key",
                    auth_style="bearer",
                    priority=0,
                    allowed_fails=1,
                    cooldown_seconds=30,
                ),
            ],
            [
                ModelChannelRouteSnapshot(
                    route_id="mcr_test_primary",
                    public_model_id="gpt-5.3-codex",
                    endpoint="responses",
                    channel_id="ch_test_primary",
                ),
            ],
        )
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {"error": {"message": "primary failed", "type": "server_error"}},
                    status_code=500,
                ),
                _FakeUpstreamResponse(
                    {
                        "id": "resp_system_fallback",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": "OK"}],
                            }
                        ],
                        "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
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
            ), patch.object(proxy_module.usage_buffer, "add", AsyncMock()) as add_usage:
                response = await client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={"model": "gpt-5.3-codex", "input": "Reply with only: OK"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(upstream_client.calls), 2)
        self.assertEqual(upstream_client.calls[0]["url"], "https://primary-channel.example/v1/responses")
        self.assertEqual(upstream_client.calls[1]["url"], "https://legacy.example/v1/responses")
        self.assertEqual(upstream_client.calls[1]["headers"]["api-key"], "legacy-key")
        self.assertEqual(upstream_client.calls[1]["json"]["model"], "gpt-5.3-codex")
        add_usage.assert_awaited_once()
        usage_kwargs = add_usage.await_args.kwargs
        self.assertEqual(usage_kwargs["route_reason"], "system_fallback:500")
        self.assertEqual(usage_kwargs["channel_id"], "system:legacy_cpa")
        self.assertEqual(usage_kwargs["channel_type"], "account_pool")
        self.assertEqual(usage_kwargs["provider_platform"], "legacy_cpa")
        self.assertEqual(usage_kwargs["fallback_from_channel_id"], "ch_test_primary")
        self.assertEqual(usage_kwargs["route_attempt"], 1)

    async def test_responses_stream_provider_channel_falls_back_to_next_channel_on_retryable_status(self) -> None:
        channel_router.set_snapshot(
            [
                ProviderChannelSnapshot(
                    channel_id="ch_stream_primary",
                    provider_platform="sub2api",
                    base_url="https://primary-channel.example/v1",
                    api_key="primary-key",
                    auth_style="bearer",
                    priority=0,
                    allowed_fails=1,
                    cooldown_seconds=30,
                ),
                ProviderChannelSnapshot(
                    channel_id="ch_stream_backup",
                    provider_platform="new_api",
                    base_url="https://backup-channel.example/v1",
                    api_key="backup-key",
                    auth_style="bearer",
                    priority=10,
                ),
            ],
            [
                ModelChannelRouteSnapshot(
                    route_id="mcr_stream_primary",
                    public_model_id="gpt-5.3-codex",
                    endpoint="responses",
                    channel_id="ch_stream_primary",
                ),
                ModelChannelRouteSnapshot(
                    route_id="mcr_stream_backup",
                    public_model_id="gpt-5.3-codex",
                    endpoint="responses",
                    channel_id="ch_stream_backup",
                ),
            ],
        )
        upstream_client = _RecordingStreamClient(
            [
                _FakeUpstreamResponse(
                    {"error": {"message": "primary failed", "type": "server_error"}},
                    status_code=500,
                ),
                _FakeEventStreamResponse(
                    [
                        'data: {"type":"response.created","response":{"id":"resp_stream_channel_fallback","status":"in_progress","model":"gpt-5.3-codex","output":[]}}',
                        'data: {"type":"response.output_text.delta","delta":"OK"}',
                        'data: {"type":"response.completed","response":{"id":"resp_stream_channel_fallback","status":"completed","model":"gpt-5.3-codex","output":[{"type":"message","content":[{"type":"output_text","text":"OK"}]}],"usage":{"input_tokens":3,"output_tokens":1,"total_tokens":4}}}',
                        "data: [DONE]",
                    ]
                ),
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
                    json={"model": "gpt-5.3-codex", "input": "Reply with only: OK", "stream": True},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("response.output_text.delta", response.text)
        self.assertEqual(len(upstream_client.calls), 2)
        self.assertEqual(upstream_client.calls[0]["url"], "https://primary-channel.example/v1/responses")
        self.assertEqual(upstream_client.calls[0]["headers"]["authorization"], "Bearer primary-key")
        self.assertEqual(upstream_client.calls[1]["url"], "https://backup-channel.example/v1/responses")
        self.assertEqual(upstream_client.calls[1]["headers"]["authorization"], "Bearer backup-key")
        add_usage.assert_awaited_once()
        usage_kwargs = add_usage.await_args.kwargs
        self.assertEqual(usage_kwargs["route_reason"], "channel_fallback:500")
        self.assertEqual(usage_kwargs["endpoint"], "responses:stream")
        self.assertEqual(usage_kwargs["channel_id"], "ch_stream_backup")
        self.assertEqual(usage_kwargs["fallback_from_channel_id"], "ch_stream_primary")
        self.assertEqual(usage_kwargs["route_attempt"], 1)

    async def test_responses_stream_provider_channel_falls_back_to_system_catalog_when_no_peer_route(self) -> None:
        channel_router.set_snapshot(
            [
                ProviderChannelSnapshot(
                    channel_id="ch_stream_primary",
                    provider_platform="sub2api",
                    base_url="https://primary-channel.example/v1",
                    api_key="primary-key",
                    auth_style="bearer",
                    priority=0,
                    allowed_fails=1,
                    cooldown_seconds=30,
                ),
            ],
            [
                ModelChannelRouteSnapshot(
                    route_id="mcr_stream_primary",
                    public_model_id="gpt-5.3-codex",
                    endpoint="responses",
                    channel_id="ch_stream_primary",
                ),
            ],
        )
        upstream_client = _RecordingStreamClient(
            [
                _FakeUpstreamResponse(
                    {"error": {"message": "primary failed", "type": "server_error"}},
                    status_code=500,
                ),
                _FakeEventStreamResponse(
                    [
                        'data: {"type":"response.created","response":{"id":"resp_stream_system_fallback","status":"in_progress","model":"gpt-5.3-codex","output":[]}}',
                        'data: {"type":"response.output_text.delta","delta":"OK"}',
                        'data: {"type":"response.completed","response":{"id":"resp_stream_system_fallback","status":"completed","model":"gpt-5.3-codex","output":[{"type":"message","content":[{"type":"output_text","text":"OK"}]}],"usage":{"input_tokens":3,"output_tokens":1,"total_tokens":4}}}',
                        "data: [DONE]",
                    ]
                ),
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
                    json={"model": "gpt-5.3-codex", "input": "Reply with only: OK", "stream": True},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("response.output_text.delta", response.text)
        self.assertEqual(len(upstream_client.calls), 2)
        self.assertEqual(upstream_client.calls[0]["url"], "https://primary-channel.example/v1/responses")
        self.assertEqual(upstream_client.calls[1]["url"], "https://legacy.example/v1/responses")
        self.assertEqual(upstream_client.calls[1]["headers"]["api-key"], "legacy-key")
        self.assertEqual(upstream_client.calls[1]["json"]["model"], "gpt-5.3-codex")
        add_usage.assert_awaited_once()
        usage_kwargs = add_usage.await_args.kwargs
        self.assertEqual(usage_kwargs["route_reason"], "system_fallback:500")
        self.assertEqual(usage_kwargs["endpoint"], "responses:stream")
        self.assertEqual(usage_kwargs["channel_id"], "system:legacy_cpa")
        self.assertEqual(usage_kwargs["provider_platform"], "legacy_cpa")
        self.assertEqual(usage_kwargs["fallback_from_channel_id"], "ch_stream_primary")
        self.assertEqual(usage_kwargs["route_attempt"], 1)

    async def test_responses_claude_code_alias_adds_prompt_cache_key(self) -> None:
        self._add_claude_code_model()
        fake_user = SimpleNamespace(id="u_test", _api_key_id="k_claude_resp")
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "resp_claude_cache",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": "OK"}],
                            }
                        ],
                        "usage": {
                            "input_tokens": 12,
                            "input_tokens_details": {"cached_tokens": 7},
                            "output_tokens": 2,
                            "total_tokens": 14,
                        },
                    }
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=fake_user)), patch.object(
                proxy_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ), patch.object(proxy_module.usage_buffer, "add", AsyncMock()) as add_usage:
                response = await client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={"model": "claude-opus-4-7", "input": "Reply with only: OK"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        request_json = upstream_client.calls[0]["json"]
        self.assertEqual(request_json["model"], "gpt-5.5")
        prompt_cache_key = request_json.get("prompt_cache_key", "")
        self.assertTrue(prompt_cache_key.startswith("cc-"))
        self.assertEqual(len(prompt_cache_key), 35)
        add_usage.assert_awaited_once()
        self.assertEqual(add_usage.await_args.kwargs["api_key_id"], "k_claude_resp")
        self.assertEqual(add_usage.await_args.kwargs["cache_read_tokens"], 7)

    async def test_chat_claude_alias_can_route_to_kiro_go_chat_endpoint(self) -> None:
        self._add_claude_code_model()
        settings.claude_compat_provider = "kiro_go"
        registry._initialized = False
        registry.init_from_settings()
        fake_user = SimpleNamespace(id="u_test", _api_key_id="k_claude_chat_kiro")
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "chatcmpl_kiro_chat",
                        "choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13},
                    }
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(openai_module, "authorize_request", AsyncMock(return_value=fake_user)), patch.object(
                openai_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ), patch.object(openai_module.usage_buffer, "add", AsyncMock()) as add_usage:
                response = await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={
                        "model": "claude-opus-4-7",
                        "messages": [{"role": "user", "content": "Reply with only: OK"}],
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["model"], "claude-opus-4-7")
        self.assertEqual(payload["choices"][0]["message"]["content"], "OK")
        self.assertEqual(upstream_client.calls[0]["url"], "https://kiro-go.example/v1/chat/completions")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "claude-opus-4.7")
        self.assertEqual(upstream_client.calls[0]["headers"]["authorization"], "Bearer kiro-key")
        add_usage.assert_awaited_once()

    async def test_responses_claude_alias_can_bridge_to_kiro_go_chat_endpoint(self) -> None:
        self._add_claude_code_model()
        settings.claude_compat_provider = "kiro_go"
        registry._initialized = False
        registry.init_from_settings()
        fake_user = SimpleNamespace(id="u_test", _api_key_id="k_claude_resp_kiro")
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "chatcmpl_kiro_resp",
                        "choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11},
                    }
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=fake_user)), patch.object(
                proxy_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ), patch.object(proxy_module.usage_buffer, "add", AsyncMock()) as add_usage:
                response = await client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={"model": "claude-opus-4-7", "input": "Reply with only: OK"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["model"], "claude-opus-4-7")
        self.assertEqual(payload["output_text"], "OK")
        self.assertEqual(payload["output"][0]["content"][0]["text"], "OK")
        self.assertEqual(upstream_client.calls[0]["url"], "https://kiro-go.example/v1/chat/completions")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "claude-opus-4.7")
        self.assertEqual(upstream_client.calls[0]["headers"]["authorization"], "Bearer kiro-key")
        add_usage.assert_awaited_once()
        self.assertEqual(add_usage.await_args.kwargs["endpoint"], "responses")

    async def test_chat_claude_alias_can_stream_from_kiro_go_chat_endpoint(self) -> None:
        self._add_claude_code_model()
        settings.claude_compat_provider = "kiro_go"
        registry._initialized = False
        registry.init_from_settings()
        fake_user = SimpleNamespace(id="u_test", _api_key_id="k_claude_chat_stream_kiro")
        upstream_client = _RecordingStreamClient(
            [
                _FakeEventStreamResponse(
                    [
                        'data: {"id":"chatcmpl_kiro_stream","object":"chat.completion.chunk","created":1700000000,"model":"claude-opus-4.7","choices":[{"index":0,"delta":{"role":"assistant","content":"OK"},"finish_reason":null}]}',
                        'data: {"id":"chatcmpl_kiro_stream","object":"chat.completion.chunk","created":1700000001,"model":"claude-opus-4.7","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":9,"completion_tokens":2,"total_tokens":11}}',
                        'data: [DONE]',
                    ]
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(openai_module, "authorize_request", AsyncMock(return_value=fake_user)), patch.object(
                openai_module,
                "get_stream_client",
                AsyncMock(return_value=upstream_client),
            ), patch.object(openai_module.usage_buffer, "add", AsyncMock()) as add_usage:
                response = await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={
                        "model": "claude-opus-4-7",
                        "stream": True,
                        "messages": [{"role": "user", "content": "Reply with only: OK"}],
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('"model":"claude-opus-4-7"', response.text)
        self.assertIn('"content":"OK"', response.text)
        self.assertEqual(upstream_client.calls[0]["url"], "https://kiro-go.example/v1/chat/completions")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "claude-opus-4.7")
        add_usage.assert_awaited_once()
        self.assertEqual(add_usage.await_args.kwargs["endpoint"], "chat/completions:stream")

    async def test_responses_claude_alias_can_stream_from_kiro_go_chat_endpoint(self) -> None:
        self._add_claude_code_model()
        settings.claude_compat_provider = "kiro_go"
        registry._initialized = False
        registry.init_from_settings()
        fake_user = SimpleNamespace(id="u_test", _api_key_id="k_claude_resp_stream_kiro")
        upstream_client = _RecordingStreamClient(
            [
                _FakeEventStreamResponse(
                    [
                        'data: {"id":"chatcmpl_kiro_resp_stream","object":"chat.completion.chunk","created":1700000000,"model":"claude-opus-4.7","choices":[{"index":0,"delta":{"role":"assistant","content":"OK"},"finish_reason":null}]}',
                        'data: {"id":"chatcmpl_kiro_resp_stream","object":"chat.completion.chunk","created":1700000001,"model":"claude-opus-4.7","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":9,"completion_tokens":2,"total_tokens":11}}',
                        'data: [DONE]',
                    ]
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=fake_user)), patch.object(
                proxy_module,
                "get_stream_client",
                AsyncMock(return_value=upstream_client),
            ), patch.object(proxy_module.usage_buffer, "add", AsyncMock()) as add_usage:
                response = await client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={
                        "model": "claude-opus-4-7",
                        "stream": True,
                        "input": "Reply with only: OK",
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('event: response.created', response.text)
        self.assertIn('event: response.output_text.delta', response.text)
        self.assertIn('"model": "claude-opus-4-7"', response.text)
        self.assertIn('"output_text": "OK"', response.text)
        self.assertEqual(upstream_client.calls[0]["url"], "https://kiro-go.example/v1/chat/completions")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "claude-opus-4.7")
        add_usage.assert_awaited_once()
        self.assertEqual(add_usage.await_args.kwargs["endpoint"], "responses:stream")

    async def test_chat_explicit_legacy_alias_does_not_fallback_to_a_different_model(self) -> None:
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {"error": {"message": "primary failed", "type": "server_error"}},
                    status_code=500,
                ),
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
                        "model": "gpt-5.2-codex",
                        "messages": [{"role": "user", "content": "Reply with only: OK"}],
                    },
                )

        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "gpt-5.3-codex")
        self.assertEqual(upstream_client.calls[0]["url"], "https://legacy.example/v1/responses")

    async def test_chat_claude_code_alias_maps_response_cached_tokens(self) -> None:
        self._add_claude_code_model()
        fake_user = SimpleNamespace(id="u_test", _api_key_id="k_claude_chat")
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "resp_claude_chat_cache",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": "OK"}],
                            }
                        ],
                        "usage": {
                            "input_tokens": 12,
                            "input_tokens_details": {"cached_tokens": 7},
                            "output_tokens": 2,
                            "total_tokens": 14,
                        },
                    }
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(openai_module, "authorize_request", AsyncMock(return_value=fake_user)), patch.object(
                openai_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ), patch.object(openai_module.usage_buffer, "add", AsyncMock()) as add_usage:
                response = await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={
                        "model": "claude-opus-4-7",
                        "messages": [{"role": "user", "content": "Reply with only: OK"}],
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["model"], "claude-opus-4-7")
        self.assertEqual(payload["usage"]["prompt_tokens_details"]["cached_tokens"], 7)
        request_json = upstream_client.calls[0]["json"]
        self.assertEqual(request_json["model"], "gpt-5.5")
        self.assertTrue(request_json.get("prompt_cache_key", "").startswith("cc-"))
        add_usage.assert_awaited_once()
        self.assertEqual(add_usage.await_args.kwargs["api_key_id"], "k_claude_chat")
        self.assertEqual(add_usage.await_args.kwargs["cache_read_tokens"], 7)

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
        settings.primary_auth_style = "bearer"
        registry._initialized = False
        registry.init_from_settings()
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "resp_empty_responses",
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
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
        add_usage.assert_awaited_once()
        self.assertEqual(add_usage.await_args.kwargs["price_input_per_million"], 250)
        self.assertEqual(add_usage.await_args.kwargs["price_output_per_million"], 1500)

    async def test_responses_alias_override_keeps_alias_prices_when_target_changes(self) -> None:
        registry.set_runtime_alias_overrides(
            {
                "gpt-5.5": {
                    "provider_model": "gpt-5.5",
                    "upstream_model": "gpt-5.5",
                }
            },
            version=1,
        )
        registry._initialized = False
        registry.init_from_settings()

        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "resp_alias_price",
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": "OK"}],
                            }
                        ],
                        "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
                    }
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                with patch.object(proxy_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                    proxy_module,
                    "get_http_client",
                    AsyncMock(return_value=upstream_client),
                ), patch.object(proxy_module.usage_buffer, "add", AsyncMock()) as add_usage:
                    response = await client.post(
                        "/v1/responses",
                        headers={"Authorization": "Bearer sk_cc_test"},
                        json={"model": "gpt-5.5", "input": "Reply with only: OK"},
                    )

                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(upstream_client.calls[0]["json"]["model"], "gpt-5.5")
                add_usage.assert_awaited_once()
                self.assertEqual(add_usage.await_args.kwargs["customer_model_alias"], "gpt-5.5")
                self.assertEqual(add_usage.await_args.kwargs["provider_model"], "gpt-5.5")
                self.assertEqual(add_usage.await_args.kwargs["price_input_per_million"], 500)
                self.assertEqual(add_usage.await_args.kwargs["price_output_per_million"], 3000)
        finally:
            registry.clear_runtime_alias_overrides()
            registry._initialized = False
            registry.init_from_settings()

    async def test_responses_explicit_codex_alias_collapses_stream_and_preserves_reasoning(self) -> None:
        settings.router_enabled = False
        settings.primary_auth_style = "bearer"
        registry._initialized = False
        registry.init_from_settings()
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "resp_codex_reasoning",
                        "status": "completed",
                        "reasoning": {"effort": "high"},
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
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
            ), patch.object(proxy_module.usage_buffer, "add", AsyncMock()) as add_usage:
                response = await client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={
                        "model": "gpt-5.3-codex",
                        "input": "Reply with only: OK",
                        "reasoning": {"effort": "high"},
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["model"], "gpt-5.3-codex")
        self.assertEqual(payload["output"][0]["content"][0]["text"], "OK")
        self.assertEqual(payload["reasoning"]["effort"], "high")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "gpt-5.3-codex")
        self.assertEqual(upstream_client.calls[0]["json"]["reasoning"]["effort"], "high")
        add_usage.assert_awaited_once()

    async def test_responses_codex_auto_review_forwards_cpa_model_id(self) -> None:
        settings.router_enabled = False
        settings.primary_auth_style = "bearer"
        registry._initialized = False
        registry.init_from_settings()
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "resp_codex_auto_review",
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
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
            ), patch.object(proxy_module.usage_buffer, "add", AsyncMock()) as add_usage:
                response = await client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={
                        "model": "codex-auto-review",
                        "input": "Reply with only: OK",
                        "reasoning": {"effort": "xhigh"},
                        "tools": [{"type": "function", "name": "approve", "parameters": {"type": "object"}}],
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["model"], "codex-auto-review")
        self.assertEqual(payload["output"][0]["content"][0]["text"], "OK")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "codex-auto-review")
        self.assertEqual(upstream_client.calls[0]["json"]["reasoning"]["effort"], "xhigh")
        self.assertEqual(upstream_client.calls[0]["json"]["tools"][0]["name"], "approve")
        add_usage.assert_awaited_once()
        self.assertEqual(add_usage.await_args.kwargs["customer_model_alias"], "codex-auto-review")
        self.assertEqual(add_usage.await_args.kwargs["provider_model"], "codex-auto-review")

    async def test_responses_preserves_reasoning_encrypted_content_upstream(self) -> None:
        encrypted_content = "gAAAAAB_reasoning_state"
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "resp_reasoning_state",
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
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
            ), patch.object(proxy_module.usage_buffer, "add", AsyncMock()):
                response = await client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={
                        "model": "gpt-5.3-codex",
                        "input": [
                            {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "Reply with only: OK"}],
                            },
                            {
                                "id": "rs_gAAAAAB_internal_reasoning_id",
                                "type": "reasoning",
                                "summary": [],
                                "encrypted_content": encrypted_content,
                                "status": "completed",
                            },
                        ],
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        upstream_input = upstream_client.calls[0]["json"]["input"]
        self.assertEqual(upstream_input[1]["encrypted_content"], encrypted_content)
        self.assertNotIn("id", upstream_input[1])

    async def test_responses_gpt_5_4_with_tools_collapses_stream_tool_calls(self) -> None:
        settings.router_enabled = False
        settings.primary_auth_style = "bearer"
        registry._initialized = False
        registry.init_from_settings()
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "resp_tool_json",
                        "status": "completed",
                        "output": [
                            {
                                "type": "function_call",
                                "id": "call_123",
                                "name": "read_file",
                                "arguments": "{\"path\":\"foo.txt\"}",
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
        self.assertEqual(upstream_client.calls[0]["json"]["tools"][0]["name"], "read_file")
        add_usage.assert_awaited_once()

    async def test_responses_gemini_cpa_lane_uses_chat_endpoint_and_returns_responses_shape(self) -> None:
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "chatcmpl_gemini",
                        "created": 1774449999,
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "GEMINI_OK",
                                }
                            }
                        ],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
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
            ), patch.object(proxy_module.usage_buffer, "add", AsyncMock()) as add_usage:
                response = await client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={"model": "gemini-fast", "input": "Reply with only: GEMINI_OK", "max_output_tokens": 20},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["model"], "gemini-fast")
        self.assertEqual(payload["output_text"], "GEMINI_OK")
        self.assertEqual(payload["output"][0]["content"][0]["text"], "GEMINI_OK")
        self.assertEqual(payload["usage"]["input_tokens"], 5)
        self.assertEqual(payload["usage"]["output_tokens"], 2)
        self.assertEqual(upstream_client.calls[0]["url"], "https://gemini-cpa.example/v1/chat/completions")
        self.assertEqual(upstream_client.calls[0]["headers"]["authorization"], "Bearer gemini-cpa-key")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "gemini-2.5-flash")
        self.assertEqual(upstream_client.calls[0]["json"]["messages"][0]["role"], "user")
        self.assertEqual(upstream_client.calls[0]["json"]["max_tokens"], 20)
        self.assertNotIn("store", upstream_client.calls[0]["json"])
        add_usage.assert_awaited_once()
        self.assertEqual(add_usage.await_args.kwargs["endpoint"], "responses")
        self.assertEqual(add_usage.await_args.kwargs["model"], "gemini-fast")

    async def test_chat_nonstream_gpt_5_4_with_tools_collapses_stream_tool_calls(self) -> None:
        settings.router_enabled = False
        settings.primary_auth_style = "bearer"
        registry._initialized = False
        registry.init_from_settings()
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "resp_tool_chat",
                        "status": "completed",
                        "output": [
                            {
                                "type": "function_call",
                                "id": "call_123",
                                "name": "read_file",
                                "arguments": "{\"path\":\"foo.txt\"}",
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

    async def test_chat_stream_waits_for_completed_usage_after_text_done(self) -> None:
        settings.router_enabled = False
        registry._initialized = False
        registry.init_from_settings()
        upstream_client = _RecordingStreamClient(
            [
                _FakeEventStreamResponse(
                    [
                        'data: {"type":"response.created","response":{"id":"resp_text_stream","status":"in_progress","model":"gpt-5.4","output":[]}}',
                        'data: {"type":"response.output_text.delta","delta":"ok"}',
                        'data: {"type":"response.output_text.done","text":"ok"}',
                        'data: {"type":"response.completed","response":{"id":"resp_text_stream","status":"completed","model":"gpt-5.4","output":[{"type":"message","content":[{"type":"output_text","text":"ok"}]}],"usage":{"input_tokens":11,"output_tokens":2,"total_tokens":13}}}',
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
                        "stream": True,
                        "messages": [{"role": "user", "content": "Say ok"}],
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('"content": "ok"', response.text)
        self.assertEqual(response.text.count('"finish_reason": "stop"'), 1)
        add_usage.assert_awaited_once()
        _, kwargs = add_usage.await_args
        self.assertEqual(kwargs["input_tokens"], 11)
        self.assertEqual(kwargs["output_tokens"], 2)
        self.assertEqual(kwargs["endpoint"], "chat/completions:stream")

    async def test_explicit_image_generation_uses_native_cpa_gemini_lane_without_vertex_key(self) -> None:
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "choices": [
                            {
                                "message": {
                                    "images": [
                                        {
                                            "image_url": {
                                                "url": "data:image/png;base64,from-cpa-gemini"
                                            }
                                        }
                                    ]
                                }
                            }
                        ]
                    },
                    headers={"x-request-id": "req_cpa_image_1"},
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
                    json={"model": "gemini-image", "prompt": "A blue coin mascot", "n": 1, "size": "1024x1024"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"][0]["b64_json"], "from-cpa-gemini")
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(upstream_client.calls[0]["url"], "https://gemini-cpa.example/v1/chat/completions")
        self.assertEqual(upstream_client.calls[0]["headers"]["authorization"], "Bearer gemini-cpa-key")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "gemini-3.1-flash-image")
        self.assertEqual(upstream_client.calls[0]["json"]["modalities"], ["image", "text"])
        self.assertEqual(upstream_client.calls[0]["json"]["messages"][0]["content"], "A blue coin mascot")

    async def test_image_generation_gateway_lane_still_supported_when_explicitly_configured(self) -> None:
        self._set_gemini_image_gateway_lane()
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
            "gemini-image",
        )
        self.assertEqual(
            upstream_client.calls[0]["headers"]["authorization"],
            "Bearer gateway-key",
        )

    async def test_openai_image_generation_without_model_uses_default_gpt_image_lane(self) -> None:
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "created": 1774449999,
                        "data": [{"b64_json": "from-openai-image"}],
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
        self.assertEqual(response.json()["data"][0]["b64_json"], "from-openai-image")
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(
            upstream_client.calls[0]["url"],
            "https://cliproxy.example/v1/images/generations",
        )
        self.assertEqual(
            upstream_client.calls[0]["json"]["model"],
            "gpt-image-2",
        )
        self.assertEqual(
            upstream_client.calls[0]["headers"]["authorization"],
            "Bearer cliproxy-key",
        )

    async def test_openai_image_generation_normalizes_root_base_url_to_v1(self) -> None:
        catalog = json.loads(settings.model_catalog_json)
        for model in catalog["models"]:
            if model.get("id") == "gpt-image-2":
                model["upstream_url"] = "https://cliproxy.example"
                break
        settings.model_catalog_json = json.dumps(catalog)
        registry._initialized = False
        registry.init_from_settings()

        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "created": 1774449999,
                        "data": [{"b64_json": "from-openai-image"}],
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
        self.assertEqual(response.json()["data"][0]["b64_json"], "from-openai-image")
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(
            upstream_client.calls[0]["url"],
            "https://cliproxy.example/v1/images/generations",
        )

    async def test_openai_image_edit_streams_direct_upstream_response(self) -> None:
        catalog = json.loads(settings.model_catalog_json)
        settings.model_catalog_json = json.dumps(catalog)
        registry._initialized = False
        registry.init_from_settings()

        upstream_client = _RecordingStreamClient(
            [
                _FakeUpstreamResponse(
                    {
                        "created": 1774449999,
                        "data": [{"b64_json": "edited-by-openai-image"}],
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
                    data={"prompt": "Make this glossy", "n": "1", "size": "1024x1024"},
                    files={"image": ("input.png", b"fake_image_data", "image/png")},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"][0]["b64_json"], "edited-by-openai-image")
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(
            upstream_client.calls[0]["url"],
            "https://cliproxy.example/v1/images/edits",
        )
        self.assertIn("multipart/form-data; boundary=", upstream_client.calls[0]["headers"]["content-type"])
        posted_body = upstream_client.calls[0]["content"].decode("utf-8", errors="replace")
        self.assertIn('name="model"', posted_body)
        self.assertIn("gpt-image-2", posted_body)
        self.assertEqual(
            upstream_client.calls[0]["headers"]["authorization"],
            "Bearer cliproxy-key",
        )

    async def test_explicit_image_generation_can_use_gemini_vertex_direct_lane(self) -> None:
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
                    json={"model": "gemini-image", "prompt": "A blue coin mascot", "n": 1, "size": "1024x1024"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(len(payload["data"]), 1)
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(
            upstream_client.calls[0]["url"],
            "https://aiplatform.googleapis.com/v1/publishers/google/models/gemini-3.1-flash-image:generateContent",
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
            "https://aiplatform.googleapis.com/v1/publishers/google/models/gemini-3.1-flash-image:generateContent",
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
        self._set_gemini_image_gateway_lane()
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
        self.assertEqual(upstream_client.calls[0]["url"], "https://gateway.example/v1/images/edits")

    async def test_image_edit_prefers_gateway_lane_when_gateway_is_configured(self) -> None:
        self._set_gemini_image_gateway_lane()
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
        self.assertIn("gemini-image", posted_body)
        self.assertEqual(
            upstream_client.calls[0]["headers"]["authorization"],
            "Bearer gateway-key",
        )

    async def test_explicit_image_edit_uses_native_cpa_gemini_lane(self) -> None:
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "choices": [
                            {
                                "message": {
                                    "images": [
                                        {
                                            "image_url": {
                                                "url": "data:image/png;base64,edited-by-cpa-gemini"
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
                    data={"model": "gemini-image", "prompt": "Turn this into pixel art", "n": "1", "size": "1024x1024"},
                    files={"image": ("input.png", b"fake_image_data", "image/png")},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"][0]["b64_json"], "edited-by-cpa-gemini")
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(upstream_client.calls[0]["url"], "https://gemini-cpa.example/v1/chat/completions")
        self.assertEqual(upstream_client.calls[0]["headers"]["authorization"], "Bearer gemini-cpa-key")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "gemini-3.1-flash-image")
        self.assertEqual(upstream_client.calls[0]["json"]["modalities"], ["image", "text"])
        content_parts = upstream_client.calls[0]["json"]["messages"][0]["content"]
        self.assertEqual(content_parts[0]["type"], "image_url")
        self.assertTrue(content_parts[0]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(content_parts[-1], {"type": "text", "text": "Turn this into pixel art"})

    async def test_image_edit_without_model_uses_default_gpt_image_lane(self) -> None:
        upstream_client = _RecordingStreamClient(
            [
                _FakeUpstreamResponse(
                    {
                        "created": 1774449999,
                        "data": [{"b64_json": "edited-by-openai-image"}],
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
        payload = response.json()
        self.assertEqual(payload["data"][0]["b64_json"], "edited-by-openai-image")
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(
            upstream_client.calls[0]["url"],
            "https://cliproxy.example/v1/images/edits",
        )
        posted_body = upstream_client.calls[0]["content"].decode("utf-8", errors="replace")
        self.assertIn("gpt-image-2", posted_body)

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
            "https://aiplatform.googleapis.com/v1/publishers/google/models/gemini-3.1-flash-image:generateContent",
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
        self._set_gemini_image_gateway_lane()

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
        self.assertEqual(upstream_client.calls[0]["url"], "https://gemini-cpa.example/v1/chat/completions")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "gemini-2.5-flash")

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
                "gpt-5.5",
                "gpt-5.1",
                "gpt-5.1-codex",
                "gpt-5.1-codex-mini",
                "gpt-5.1-codex-max",
                "gpt-5.2",
                "gpt-5.2-codex",
                "gpt-5.3-codex",
                "gpt-5.3-codex-spark",
                "codex-auto-review",
                "gpt-5.4-mini",
                "gpt-5-codex",
                "gpt-5-codex-mini",
                "text-embedding-3-small",
                "gpt-image-2",
                "gemini-fast",
                "gemini-image",
            ],
        )
        self.assertEqual(payload["data"][0]["coincoin_billable_sku"], "gpt-5.4")
        self.assertEqual(payload["data"][0]["coincoin_default_for"], ["text"])
        self.assertEqual(payload["data"][0]["coincoin_price_input_per_million"], 250)
        self.assertEqual(payload["data"][0]["coincoin_price_output_per_million"], 1500)
        self.assertEqual(payload["data"][0]["coincoin_price_cached_input_per_million"], 25.0)
        self.assertNotIn("coincoin_provider_model", payload["data"][0])
        self.assertNotIn("coincoin_provider", payload["data"][0])
        self.assertEqual(payload["data"][8]["coincoin_billable_sku"], "gpt-5.2-codex")
        self.assertEqual(payload["data"][10]["id"], "gpt-5.3-codex-spark")
        self.assertEqual(payload["data"][10]["created"], 1770912000)
        self.assertEqual(payload["data"][10]["coincoin_metadata"]["display_name"], "GPT 5.3 Codex Spark")
        self.assertEqual(payload["data"][10]["coincoin_metadata"]["context_length"], 128000)
        self.assertEqual(payload["data"][11]["id"], "codex-auto-review")
        self.assertEqual(payload["data"][11]["created"], 1776902400)
        self.assertEqual(payload["data"][11]["coincoin_billable_sku"], "codex-auto-review")
        self.assertEqual(payload["data"][11]["coincoin_delivery_lane"], "legacy")
        self.assertEqual(payload["data"][11]["coincoin_metadata"]["supported_parameters"], ["tools"])
        self.assertEqual(payload["data"][11]["coincoin_metadata"]["thinking"]["levels"], ["low", "medium", "high", "xhigh"])
        self.assertEqual(payload["data"][15]["coincoin_capabilities"], ["embeddings"])
        self.assertEqual(payload["data"][15]["coincoin_billable_sku"], "azure-text-embedding-3-small")
        self.assertEqual(payload["data"][15]["coincoin_default_for"], ["embedding"])
        self.assertEqual(payload["data"][15]["coincoin_delivery_lane"], "upstream_direct")
        self.assertEqual(payload["data"][16]["coincoin_capabilities"], ["images/generations", "images/edits"])
        self.assertEqual(payload["data"][16]["coincoin_billable_sku"], "openai-image")
        self.assertEqual(payload["data"][16]["coincoin_default_for"], ["image"])
        self.assertEqual(payload["data"][16]["coincoin_delivery_lane"], "upstream_direct")
        self.assertEqual(payload["data"][17]["coincoin_capabilities"], ["chat/completions", "responses"])
        self.assertEqual(payload["data"][17]["coincoin_billable_sku"], "gemini-fast")
        self.assertEqual(payload["data"][17]["coincoin_delivery_lane"], "cpa_gemini")
        self.assertEqual(payload["data"][18]["coincoin_capabilities"], ["images/generations", "images/edits"])
        self.assertEqual(payload["data"][18]["coincoin_default_for"], [])
        self.assertEqual(payload["data"][18]["coincoin_delivery_lane"], "cpa_gemini")

    async def test_models_endpoint_returns_station_scoped_aliases_for_station_key(self) -> None:
        station_models = [
            {
                "id": "fast",
                "object": "model",
                "created": 1700000000,
                "owned_by": "station",
                "coincoin_station_id": "st_1",
                "coincoin_station_alias": "fast",
                "coincoin_resolved_public_model": "gpt-5.4-mini",
                "coincoin_capabilities": ["chat/completions", "responses"],
                "coincoin_billable_sku": "legacy-gpt-5.4-mini-text",
                "coincoin_routing_mode": "station_alias",
                "coincoin_default_for": ["text"],
                "coincoin_price_input_per_million": 120,
                "coincoin_price_cached_input_per_million": 12.0,
                "coincoin_price_output_per_million": 720,
                "coincoin_price_per_image_cents": 0,
            }
        ]
        fake_user = SimpleNamespace(id="u_station_child", _station_context={"station_id": "st_1", "status": "active"})

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(openai_module, "authenticate_user", AsyncMock(return_value=fake_user)), patch(
                "app.stations.list_station_public_models_for_user",
                AsyncMock(return_value=station_models),
            ):
                response = await client.get("/v1/models", headers={"Authorization": "Bearer sk_station"})

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["object"], "list")
        self.assertEqual([item["id"] for item in payload["data"]], ["fast"])
        self.assertEqual(payload["data"][0]["owned_by"], "station")
        self.assertEqual(payload["data"][0]["coincoin_resolved_public_model"], "gpt-5.4-mini")
        self.assertEqual(payload["data"][0]["coincoin_price_output_per_million"], 720)

    async def test_balance_returns_station_price_context_for_station_session(self) -> None:
        station_models = [
            {
                "id": "fast",
                "object": "model",
                "created": 1700000000,
                "owned_by": "station",
                "coincoin_station_id": "st_1",
                "coincoin_station_alias": "fast",
                "coincoin_resolved_public_model": "gpt-5.4-mini",
                "coincoin_capabilities": ["chat/completions", "responses"],
                "coincoin_billable_sku": "legacy-gpt-5.4-mini-text",
                "coincoin_routing_mode": "station_alias",
                "coincoin_default_for": ["text"],
                "coincoin_price_input_per_million": 120,
                "coincoin_price_cached_input_per_million": 12.0,
                "coincoin_price_output_per_million": 720,
                "coincoin_price_per_image_cents": 0,
            }
        ]
        fake_user = SimpleNamespace(
            id="u_station_child",
            _station_context={"station_id": "st_1", "slug": "stone", "display_name": "Stone AI", "status": "active"},
        )
        db_user = SimpleNamespace(
            id="u_station_child",
            balance=1234,
            token_used=0,
            input_tokens_used=0,
            output_tokens_used=0,
            token_limit=None,
        )
        fake_db = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: db_user))
        )

        with patch.object(openai_module, "authenticate_user", AsyncMock(return_value=fake_user)), patch.object(
            openai_module.usage_buffer, "get_pending_tokens", AsyncMock(return_value=0)
        ), patch.object(openai_module.usage_buffer, "get_pending_cost", AsyncMock(return_value=0)), patch(
            "app.stations.get_station_public_models_by_id",
            AsyncMock(return_value=station_models),
        ):
            result = await openai_module.get_balance(request=SimpleNamespace(headers={}), db=fake_db)

        self.assertEqual(result.pricing_scope, "station")
        self.assertEqual(result.pricing_model_id, "fast")
        self.assertEqual(result.station_slug, "stone")
        self.assertEqual(result.station_display_name, "Stone AI")
        self.assertEqual(result.price_input_per_million, 1.2)
        self.assertEqual(result.price_output_per_million, 7.2)
        self.assertEqual(result.station_pricing_models[0]["id"], "fast")

    async def test_openai_prefixed_models_endpoint_returns_curated_metadata(self) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/openai/v1/models")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["object"], "list")
        self.assertEqual(payload["data"][0]["id"], "gpt-5.4")
        model_ids = [item["id"] for item in payload["data"]]
        self.assertIn("gemini-fast", model_ids)

    async def test_model_detail_endpoint_returns_curated_metadata(self) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/v1/models/gemini-fast")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["id"], "gemini-fast")
        self.assertEqual(payload["object"], "model")
        self.assertEqual(payload["owned_by"], "google")
        self.assertEqual(payload["coincoin_capabilities"], ["chat/completions", "responses"])
        self.assertEqual(payload["coincoin_billable_sku"], "gemini-fast")
        self.assertEqual(payload["coincoin_routing_mode"], "direct")
        self.assertEqual(payload["coincoin_delivery_lane"], "cpa_gemini")
        self.assertEqual(payload["coincoin_price_cached_input_per_million"], 0.0)
        self.assertNotIn("coincoin_provider_model", payload)
        self.assertNotIn("coincoin_provider", payload)

    async def test_model_detail_endpoint_returns_openai_error_for_unknown_model(self) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/v1/models/not-a-real-model")

        self.assertEqual(response.status_code, 404, response.text)
        payload = response.json()
        self.assertEqual(payload["error"]["type"], "invalid_request_error")
        self.assertEqual(payload["error"]["param"], "model")
        self.assertEqual(payload["error"]["code"], "model_not_found")

    async def test_openai_prefixed_chat_completions_alias_matches_v1_behavior(self) -> None:
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "id": "resp_prefixed_chat",
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
                    "/openai/v1/chat/completions",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={"messages": [{"role": "user", "content": "Reply with only: OK"}]},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["model"], "gpt-5.4")
        self.assertEqual(payload["choices"][0]["message"]["content"], "OK")
        self.assertEqual(upstream_client.calls[0]["url"], "https://legacy.example/v1/responses")

    async def test_openai_prefixed_embeddings_alias_matches_v1_behavior(self) -> None:
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
                    "/openai/v1/embeddings",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={"input": "hello"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["model"], "text-embedding-3-small")
        self.assertEqual(upstream_client.calls[0]["url"], "https://fallback.example/v1/embeddings")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "text-embedding-3-small")


if __name__ == "__main__":
    unittest.main()
