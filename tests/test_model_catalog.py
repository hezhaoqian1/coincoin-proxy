import json
import unittest

from app.config import settings
from app.router import ModelCapabilityError, registry


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
        "provider_model": model_id,
        "capabilities": ["chat/completions", "responses"],
        "routing_mode": "legacy_auto",
        "delivery_lane": "legacy",
    }


class ModelCatalogTests(unittest.TestCase):
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
            "gateway_auth_style": settings.gateway_auth_style,
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
        settings.gateway_auth_style = "bearer"
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
                        "billable_sku": "gemini-fast-text",
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
        self.assertEqual(resolved.public_model.delivery_lane, "gateway")
        self.assertEqual(resolved.backend.model_id, "gemini-fast")
        self.assertEqual(resolved.backend.upstream_url, "https://gateway.example/v1")
        self.assertEqual(resolved.backend.auth_style, "bearer")
        self.assertEqual(resolved.route_reason, "catalog:gemini-fast:gateway")

    def test_explicit_legacy_public_model_keeps_legacy_lane(self) -> None:
        resolved = registry.resolve_public_model(
            "gpt-5.2-codex",
            "chat/completions",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
        )

        self.assertEqual(resolved.public_model.public_id, "gpt-5.2-codex")
        self.assertEqual(resolved.backend.model_id, "gpt-5.2-codex")
        self.assertEqual(resolved.route_reason, "catalog:gpt-5.2-codex:legacy_explicit")
        self.assertTrue(resolved.lock_model_selection)

    def test_explicit_gpt_5_2_alias_keeps_legacy_lane(self) -> None:
        resolved = registry.resolve_public_model(
            "gpt-5.2",
            "responses",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
        )

        self.assertEqual(resolved.public_model.public_id, "gpt-5.2")
        self.assertEqual(resolved.backend.model_id, "gpt-5.2")
        self.assertEqual(resolved.route_reason, "catalog:gpt-5.2:legacy_explicit")
        self.assertTrue(resolved.lock_model_selection)

    def test_explicit_gpt_5_4_mini_alias_keeps_legacy_lane(self) -> None:
        resolved = registry.resolve_public_model(
            "gpt-5.4-mini",
            "responses",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
        )

        self.assertEqual(resolved.public_model.public_id, "gpt-5.4-mini")
        self.assertEqual(resolved.backend.model_id, "gpt-5.4-mini")
        self.assertEqual(resolved.route_reason, "catalog:gpt-5.4-mini:legacy_explicit")
        self.assertTrue(resolved.lock_model_selection)

    def test_catalog_lists_all_expected_legacy_gpt_aliases(self) -> None:
        text_model_ids = [
            model.public_id
            for model in registry.list_public_models("chat/completions")
            if model.routing_mode == "legacy_auto"
        ]

        self.assertEqual(text_model_ids, LEGACY_PUBLIC_TEXT_MODELS)

    def test_default_image_model_is_used_when_model_is_omitted(self) -> None:
        resolved = registry.resolve_public_model(None, "images/generations")

        self.assertEqual(resolved.public_model.public_id, "gemini-image")
        self.assertEqual(resolved.public_model.delivery_lane, "gateway")
        self.assertEqual(resolved.backend.model_id, "vertex-gemini-3.1-flash-image-preview")
        self.assertEqual(resolved.backend.upstream_url, "https://gateway.example/v1")
        self.assertEqual(resolved.route_reason, "catalog:gemini-image:gateway")

    def test_default_image_model_supports_image_edits(self) -> None:
        resolved = registry.resolve_public_model(None, "images/edits")

        self.assertEqual(resolved.public_model.public_id, "gemini-image")
        self.assertEqual(resolved.public_model.delivery_lane, "gateway")
        self.assertEqual(resolved.backend.model_id, "vertex-gemini-3.1-flash-image-preview")
        self.assertEqual(resolved.backend.upstream_url, "https://gateway.example/v1")
        self.assertEqual(resolved.route_reason, "catalog:gemini-image:gateway")

    def test_default_embedding_model_uses_dedicated_azure_lane(self) -> None:
        resolved = registry.resolve_public_model(None, "embeddings")

        self.assertEqual(resolved.public_model.public_id, "text-embedding-3-small")
        self.assertEqual(resolved.public_model.delivery_lane, "upstream_direct")
        self.assertEqual(resolved.backend.model_id, "text-embedding-3-small")
        self.assertEqual(resolved.backend.upstream_url, "https://fallback.example/v1")
        self.assertEqual(resolved.backend.auth_style, "azure")
        self.assertEqual(resolved.route_reason, "catalog:text-embedding-3-small:upstream_direct")

    def test_explicit_embedding_model_uses_dedicated_azure_lane(self) -> None:
        resolved = registry.resolve_public_model("text-embedding-3-small", "embeddings")

        self.assertEqual(resolved.public_model.public_id, "text-embedding-3-small")
        self.assertEqual(resolved.backend.model_id, "text-embedding-3-small")
        self.assertEqual(resolved.backend.upstream_url, "https://fallback.example/v1")
        self.assertEqual(resolved.backend.auth_style, "azure")
        self.assertEqual(resolved.route_reason, "catalog:text-embedding-3-small:upstream_direct")

    def test_image_model_rejects_chat_endpoint(self) -> None:
        with self.assertRaises(ModelCapabilityError):
            registry.resolve_public_model("gemini-image", "chat/completions")

    def test_legacy_text_model_rejects_embeddings_endpoint(self) -> None:
        with self.assertRaises(ModelCapabilityError):
            registry.resolve_public_model("gpt-5.2-codex", "embeddings")


if __name__ == "__main__":
    unittest.main()
