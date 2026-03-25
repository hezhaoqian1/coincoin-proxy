import asyncio
import os
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


class CoinCoinVertexGatewayLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not _env_flag("COINCOIN_RUN_LIVE_VERTEX_TESTS"):
            raise unittest.SkipTest("set COINCOIN_RUN_LIVE_VERTEX_TESTS=1 to run live Vertex gateway tests")

        gateway_url = os.getenv("COINCOIN_LIVE_GATEWAY_URL", "").strip()
        gateway_key = os.getenv("COINCOIN_LIVE_GATEWAY_KEY", "").strip()
        if not gateway_url or not gateway_key:
            raise unittest.SkipTest(
                "set COINCOIN_LIVE_GATEWAY_URL and COINCOIN_LIVE_GATEWAY_KEY to run live Vertex gateway tests"
            )

        os.environ.setdefault("COINCOIN_DB_HOST", "localhost")
        os.environ.setdefault("COINCOIN_DB_NAME", "test")
        os.environ.setdefault("COINCOIN_DB_USER", "test")
        os.environ.setdefault("COINCOIN_DB_PASSWORD", "test")

        from app.main import app
        from app.config import settings
        from app.router import registry
        import app.proxy as proxy_module
        import app.openai_compat as openai_module

        cls.app = app
        cls.settings = settings
        cls.registry = registry
        cls.proxy_module = proxy_module
        cls.openai_module = openai_module
        cls.headers = {"Authorization": "Bearer sk_cc_live_test"}
        cls.fake_user = SimpleNamespace(id="u_live_vertex")
        cls._original_settings = {
            "fixed_model": settings.fixed_model,
            "router_enabled": settings.router_enabled,
            "upstream_base_url": settings.upstream_base_url,
            "upstream_api_key": settings.upstream_api_key,
            "primary_auth_style": settings.primary_auth_style,
            "cheap_model": settings.cheap_model,
            "cheap_upstream_url": settings.cheap_upstream_url,
            "cheap_api_key": settings.cheap_api_key,
            "cheap_price_input": settings.cheap_price_input,
            "cheap_price_output": settings.cheap_price_output,
            "fallback_model": settings.fallback_model,
            "fallback_upstream_url": settings.fallback_upstream_url,
            "fallback_api_key": settings.fallback_api_key,
            "fallback_auth_style": settings.fallback_auth_style,
            "gateway_base_url": settings.gateway_base_url,
            "gateway_api_key": settings.gateway_api_key,
            "gateway_auth_style": settings.gateway_auth_style,
            "model_catalog_path": settings.model_catalog_path,
            "model_catalog_json": settings.model_catalog_json,
        }

        settings.fixed_model = "gpt-5.2-codex"
        settings.router_enabled = True
        settings.upstream_base_url = "https://legacy.example/v1"
        settings.upstream_api_key = "legacy-key"
        settings.primary_auth_style = "azure"
        settings.cheap_model = "gpt-4o-mini"
        settings.cheap_upstream_url = "https://legacy.example/v1"
        settings.cheap_api_key = "legacy-key"
        settings.cheap_price_input = 15
        settings.cheap_price_output = 60
        settings.fallback_model = ""
        settings.fallback_upstream_url = ""
        settings.fallback_api_key = ""
        settings.fallback_auth_style = ""
        settings.gateway_base_url = gateway_url.rstrip("/")
        settings.gateway_api_key = gateway_key
        settings.gateway_auth_style = "bearer"
        settings.model_catalog_path = "config/model_catalog.json"
        settings.model_catalog_json = ""

        registry._initialized = False
        registry.init_from_settings()

        async def fake_get_db():
            yield None

        app.dependency_overrides[proxy_module.get_db] = fake_get_db
        app.dependency_overrides[openai_module.get_db] = fake_get_db

    @classmethod
    def tearDownClass(cls) -> None:
        for key, value in cls._original_settings.items():
            setattr(cls.settings, key, value)
        cls.registry._initialized = False
        cls.app.dependency_overrides.pop(cls.proxy_module.get_db, None)
        cls.app.dependency_overrides.pop(cls.openai_module.get_db, None)
        try:
            loop = asyncio.get_event_loop_policy().get_event_loop()
        except RuntimeError:
            loop = None
        if loop is not None and not loop.is_closed():
            loop.close()

    @asynccontextmanager
    async def _session(self):
        await self.proxy_module.close_http_client()
        transport = httpx.ASGITransport(app=self.app)
        client = httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=180.0)
        proxy_auth_patch = patch.object(
            self.proxy_module,
            "authorize_request",
            AsyncMock(return_value=self.fake_user),
        )
        openai_auth_patch = patch.object(
            self.openai_module,
            "authorize_request",
            AsyncMock(return_value=self.fake_user),
        )
        proxy_auth_patch.start()
        openai_auth_patch.start()
        try:
            yield client
        finally:
            proxy_auth_patch.stop()
            openai_auth_patch.stop()
            await client.aclose()
            await self.proxy_module.close_http_client()

    def test_public_models_endpoint_exposes_curated_catalog(self) -> None:
        asyncio.run(self._test_public_models_endpoint_exposes_curated_catalog())

    async def _test_public_models_endpoint_exposes_curated_catalog(self) -> None:
        async with self._session() as client:
            response = await client.get("/v1/models")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        model_ids = {item["id"] for item in payload["data"]}

        self.assertIn("gpt-5.2-codex", model_ids)
        self.assertIn("gemini-fast", model_ids)
        self.assertIn("vertex-gemini-3.1-pro-preview", model_ids)
        self.assertIn("gemini-image", model_ids)

    def test_chat_completions_routes_all_direct_text_models(self) -> None:
        asyncio.run(self._test_chat_completions_routes_all_direct_text_models())

    async def _test_chat_completions_routes_all_direct_text_models(self) -> None:
        text_models = [
            model.public_id
            for model in self.registry.list_public_models("chat/completions")
            if model.routing_mode != "legacy_auto"
        ]

        async with self._session() as client:
            for model_id in text_models:
                with self.subTest(model=model_id):
                    response = await client.post(
                        "/v1/chat/completions",
                        headers=self.headers,
                        json={
                            "model": model_id,
                            "messages": [{"role": "user", "content": "Reply with only: OK"}],
                            "max_tokens": 8,
                        },
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    payload = response.json()
                    self.assertEqual(payload["model"], model_id)
                    self.assertTrue(payload["choices"])
                    self.assertEqual(payload["choices"][0]["message"]["role"], "assistant")
                    self.assertGreaterEqual(payload["usage"]["total_tokens"], 1)

    def test_responses_routes_stable_and_preview_aliases(self) -> None:
        asyncio.run(self._test_responses_routes_stable_and_preview_aliases())

    async def _test_responses_routes_stable_and_preview_aliases(self) -> None:
        async with self._session() as client:
            for model_id in ("gemini-balanced", "vertex-gemini-3.1-pro-preview"):
                with self.subTest(model=model_id):
                    response = await client.post(
                        "/v1/responses",
                        headers=self.headers,
                        json={
                            "model": model_id,
                            "input": "Reply with only: OK",
                            "max_output_tokens": 8,
                        },
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    payload = response.json()
                    self.assertEqual(payload["model"], model_id)
                    self.assertTrue(str(payload["id"]).startswith("resp_"))
                    self.assertTrue(payload["output"])
                    self.assertGreaterEqual(payload["usage"]["total_tokens"], 1)

    def test_image_generation_routes_all_curated_image_models(self) -> None:
        asyncio.run(self._test_image_generation_routes_all_curated_image_models())

    async def _test_image_generation_routes_all_curated_image_models(self) -> None:
        image_models = [
            model.public_id
            for model in self.registry.list_public_models("images/generations")
        ]

        async with self._session() as client:
            for model_id in image_models:
                with self.subTest(model=model_id):
                    response = await client.post(
                        "/v1/images/generations",
                        headers=self.headers,
                        json={
                            "model": model_id,
                            "prompt": "A minimal blue coin icon on a white background",
                            "n": 1,
                            "size": "1024x1024",
                        },
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    payload = response.json()
                    self.assertTrue(payload["data"])


if __name__ == "__main__":
    unittest.main()
