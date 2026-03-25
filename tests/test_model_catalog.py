import json
import unittest

from app.config import settings
from app.router import ModelCapabilityError, registry


class ModelCatalogTests(unittest.TestCase):
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

        settings.fixed_model = "gpt-5.4"
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
        settings.gateway_auth_style = "bearer"
        settings.model_catalog_json = json.dumps(
            {
                "default_text_model": "gpt-5.4",
                "default_image_model": "gemini-image",
                "models": [
                    {
                        "id": "gpt-5.4",
                        "owned_by": "openai",
                        "provider_name": "OpenAI",
                        "capabilities": ["chat/completions", "responses", "embeddings"],
                        "routing_mode": "legacy_auto",
                    },
                    {
                        "id": "gpt-5.2",
                        "owned_by": "openai",
                        "provider_name": "OpenAI",
                        "capabilities": ["chat/completions", "responses", "embeddings"],
                        "routing_mode": "legacy_auto",
                    },
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
                        "billable_sku": "gemini-fast-text",
                    },
                    {
                        "id": "gemini-image",
                        "owned_by": "google",
                        "provider_name": "Google",
                        "provider_model": "gemini-3.1-flash-image-preview",
                        "capabilities": ["images/generations", "images/edits"],
                        "routing_mode": "direct",
                        "upstream_model": "vertex-gemini-3.1-flash-image-preview",
                        "upstream_url": "https://gateway.example/v1",
                        "api_key": "gateway-key",
                        "auth_style": "bearer",
                        "price_per_image_cents": 7,
                        "billable_sku": "gemini-image",
                    },
                ],
            }
        )
        registry._initialized = False
        registry.init_from_settings()

    def tearDown(self) -> None:
        for key, value in self._originals.items():
            setattr(settings, key, value)
        registry._initialized = False

    def test_default_text_model_keeps_legacy_public_alias(self) -> None:
        resolved = registry.resolve_public_model(
            None,
            "chat/completions",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
        )

        self.assertEqual(resolved.public_model.public_id, "gpt-5.4")
        self.assertEqual(resolved.backend.model_id, "gpt-4o-mini")
        self.assertEqual(resolved.backend.auth_style, "azure")

    def test_explicit_gemini_text_model_uses_gateway_route(self) -> None:
        resolved = registry.resolve_public_model("gemini-fast", "responses")

        self.assertEqual(resolved.public_model.public_id, "gemini-fast")
        self.assertEqual(resolved.public_model.provider_name, "Google")
        self.assertEqual(resolved.backend.model_id, "gemini-fast")
        self.assertEqual(resolved.backend.upstream_url, "https://gateway.example/v1")
        self.assertEqual(resolved.backend.auth_style, "bearer")

    def test_explicit_legacy_public_model_keeps_legacy_lane(self) -> None:
        resolved = registry.resolve_public_model(
            "gpt-5.2-codex",
            "chat/completions",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
        )

        self.assertEqual(resolved.public_model.public_id, "gpt-5.2-codex")
        self.assertEqual(resolved.backend.model_id, "gpt-4o-mini")
        self.assertEqual(resolved.route_reason, "catalog:gpt-5.2-codex:auto_cheap")

    def test_explicit_gpt_5_2_alias_keeps_legacy_lane(self) -> None:
        resolved = registry.resolve_public_model(
            "gpt-5.2",
            "responses",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
        )

        self.assertEqual(resolved.public_model.public_id, "gpt-5.2")
        self.assertEqual(resolved.backend.model_id, "gpt-4o-mini")
        self.assertEqual(resolved.route_reason, "catalog:gpt-5.2:auto_cheap")

    def test_default_image_model_is_used_when_model_is_omitted(self) -> None:
        resolved = registry.resolve_public_model(None, "images/generations")

        self.assertEqual(resolved.public_model.public_id, "gemini-image")
        self.assertEqual(resolved.backend.model_id, "vertex-gemini-3.1-flash-image-preview")
        self.assertEqual(resolved.backend.upstream_url, "https://gateway.example/v1")

    def test_default_image_model_supports_image_edits(self) -> None:
        resolved = registry.resolve_public_model(None, "images/edits")

        self.assertEqual(resolved.public_model.public_id, "gemini-image")
        self.assertEqual(resolved.backend.model_id, "vertex-gemini-3.1-flash-image-preview")
        self.assertEqual(resolved.backend.upstream_url, "https://gateway.example/v1")

    def test_image_model_rejects_chat_endpoint(self) -> None:
        with self.assertRaises(ModelCapabilityError):
            registry.resolve_public_model("gemini-image", "chat/completions")


if __name__ == "__main__":
    unittest.main()
