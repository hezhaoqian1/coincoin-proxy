import json
import os
import tempfile
import unittest
from unittest.mock import patch

from app.config import settings
from app.model_alias_overrides import override_rows_to_snapshot
from app.router import ModelCapabilityError, _resolve_placeholders, registry


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
    "gpt-5.6",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
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
    "gpt-5.6": (500, 3000),
    "gpt-5.6-sol": (500, 3000),
    "gpt-5.6-terra": (250, 1500),
    "gpt-5.6-luna": (100, 600),
    "gpt-5-codex": (175, 1400),
    "gpt-5-codex-mini": (75, 450),
}


def _legacy_text_model(model_id: str) -> dict:
    provider_model_aliases = {
        "gpt-5.2-codex": "gpt-5.3-codex",
        "gpt-5.6": "gpt-5.6-sol",
    }
    provider_model = provider_model_aliases.get(model_id, model_id)
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
    if model_id in {"gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}:
        model["pricing"] = {"cache_creation_multiplier": 1.25}
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
            "gemini_cpa_auth_style": settings.gemini_cpa_auth_style,
            "claude_compat_provider": settings.claude_compat_provider,
            "claude_compat_base_url": settings.claude_compat_base_url,
            "claude_compat_api_key": settings.claude_compat_api_key,
            "claude_compat_auth_style": settings.claude_compat_auth_style,
            "model_catalog_json": settings.model_catalog_json,
            "model_alias_overrides_path": settings.model_alias_overrides_path,
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
        settings.gateway_auth_style = "bearer"
        settings.gemini_cpa_auth_style = "bearer"
        settings.claude_compat_provider = "upstream_direct"
        settings.claude_compat_base_url = "https://kiro-go.example"
        settings.claude_compat_api_key = "kiro-key"
        settings.claude_compat_auth_style = "bearer"
        settings.model_alias_overrides_path = ""
        settings.model_catalog_json = json.dumps(
            {
                "default_text_model": "gpt-5.4",
                "default_embedding_model": "text-embedding-3-small",
                "default_image_model": "gpt-image-2",
                "default_video_model": "seedance-v2-720p",
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
                        "upstream_url": "https://fallback.example/v1",
                        "api_key": "fallback-key",
                        "auth_style": "azure",
                        "price_per_image_cents": 5.3,
                        "billable_sku": "openai-image",
                    },
                    {
                        "id": "claude-opus-4-8",
                        "owned_by": "coincoin",
                        "provider_name": "",
                        "provider_model": "${COINCOIN_FIXED_MODEL}",
                        "capabilities": ["chat/completions", "responses"],
                        "routing_mode": "direct",
                        "delivery_lane": "upstream_direct",
                        "upstream_model": "${COINCOIN_FIXED_MODEL}",
                        "upstream_url": "https://legacy-claude.example/v1",
                        "api_key": "legacy-claude-key",
                        "auth_style": "bearer",
                        "price_input_per_million": 500,
                        "price_output_per_million": 2500,
                        "billable_sku": "claude-code-compat-text",
                        "metadata": {"compat_family": "claude-code"},
                    },
                    {
                        "id": "claude-opus-4-7",
                        "owned_by": "coincoin",
                        "provider_name": "",
                        "provider_model": "claude-opus-4.7",
                        "capabilities": ["chat/completions", "responses"],
                        "routing_mode": "direct",
                        "delivery_lane": "upstream_direct",
                        "upstream_model": "claude-opus-4.7",
                        "upstream_url": "https://legacy-claude.example/v1",
                        "api_key": "legacy-claude-key",
                        "auth_style": "bearer",
                        "price_input_per_million": 500,
                        "price_output_per_million": 2500,
                        "billable_sku": "claude-code-compat-text",
                        "metadata": {"compat_family": "claude-code"},
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
                        "price_input_per_million": 10,
                        "price_output_per_million": 40,
                        "billable_sku": "gemini-fast-text",
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
                        "price_per_image_cents": 7,
                        "billable_sku": "gemini-image",
                        "metadata": {"provider_platform": "cpa_gemini"},
                    },
                    {
                        "id": "seedance-v2-720p",
                        "owned_by": "bytedance",
                        "provider_name": "Seedance",
                        "provider_model": "seedance-v2-720p",
                        "capabilities": ["videos/generations"],
                        "routing_mode": "direct",
                        "delivery_lane": "upstream_direct",
                        "upstream_model": "seedance-v2-720p",
                        "upstream_url": "https://api.wgspai.cn",
                        "api_key": "seedance-key",
                        "auth_style": "bearer",
                        "price_per_video_cents": 98,
                        "billable_sku": "seedance-v2-720p-video-task",
                    },
                ],
            }
        )
        registry._initialized = False
        registry.init_from_settings()

    def tearDown(self) -> None:
        for key, value in self._originals.items():
            setattr(settings, key, value)
        registry.clear_runtime_alias_overrides()
        registry.clear_runtime_pricing_overrides()
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
        self.assertEqual(resolved.execution_profile, "legacy_general")
        self.assertEqual(resolved.execution_pool, "cpa_general_pool")

    def test_explicit_gemini_text_model_uses_native_cpa_route(self) -> None:
        resolved = registry.resolve_public_model("gemini-fast", "responses")

        self.assertEqual(resolved.public_model.public_id, "gemini-fast")
        self.assertEqual(resolved.public_model.provider_name, "Google")
        self.assertEqual(resolved.public_model.delivery_lane, "cpa_gemini")
        self.assertEqual(resolved.backend.model_id, "gemini-2.5-flash")
        self.assertEqual(resolved.backend.upstream_url, "https://gemini-cpa.example/v1")
        self.assertEqual(resolved.backend.auth_style, "bearer")
        self.assertEqual(resolved.execution_profile, "cpa_gemini_direct")
        self.assertEqual(resolved.execution_pool, "cpa_gemini_direct_pool")
        self.assertEqual(resolved.route_reason, "catalog:gemini-fast:cpa_gemini")

    def test_public_model_pricing_multiplier_compiles_effective_prices(self) -> None:
        settings.model_catalog_json = json.dumps(
            {
                "default_text_model": "priced-fast",
                "default_video_model": "priced-video",
                "models": [
                    {
                        "id": "priced-fast",
                        "owned_by": "coincoin",
                        "provider_model": "gemini-2.5-flash",
                        "capabilities": ["chat/completions", "responses"],
                        "routing_mode": "direct",
                        "delivery_lane": "cpa_gemini",
                        "upstream_model": "gemini-2.5-flash",
                        "upstream_url": "https://gemini-cpa.example/v1",
                        "api_key": "gemini-cpa-key",
                        "auth_style": "bearer",
                        "price_input_per_million": 100,
                        "price_output_per_million": 200,
                        "pricing": {
                            "model_multiplier": 1.5,
                            "output_multiplier": 2,
                            "cache_read_multiplier": 0.2,
                            "price_version": 7,
                        },
                    },
                    {
                        "id": "priced-video",
                        "owned_by": "bytedance",
                        "provider_model": "seedance-v2-720p",
                        "capabilities": ["videos/generations"],
                        "routing_mode": "direct",
                        "delivery_lane": "upstream_direct",
                        "upstream_model": "seedance-v2-720p",
                        "upstream_url": "https://api.wgspai.cn",
                        "api_key": "seedance-key",
                        "auth_style": "bearer",
                        "price_per_video_cents": 98,
                        "pricing": {
                            "video_multiplier": 1.25,
                            "price_version": 8,
                        },
                    }
                ],
            }
        )
        registry._initialized = False
        registry.init_from_settings()

        public_model = registry.get_public_model("priced-fast")

        self.assertEqual(public_model.base_price_input_per_million, 100)
        self.assertEqual(public_model.base_price_output_per_million, 200)
        self.assertEqual(public_model.price_input_per_million, 150)
        self.assertEqual(public_model.price_output_per_million, 600)
        self.assertEqual(public_model.model_multiplier, 1.5)
        self.assertEqual(public_model.output_multiplier, 2)
        self.assertEqual(public_model.cache_read_multiplier, 0.2)
        self.assertEqual(public_model.price_version, 7)

        video_model = registry.get_public_model("priced-video")

        self.assertEqual(video_model.base_price_per_video_cents, 98)
        self.assertEqual(video_model.price_per_video_cents, 122.5)
        self.assertEqual(video_model.video_multiplier, 1.25)
        self.assertEqual(video_model.price_version, 8)

    def test_runtime_pricing_override_changes_effective_prices_without_alias_route(self) -> None:
        registry.set_runtime_pricing_overrides(
            {
                "gemini-fast": {
                    "model_multiplier": 2,
                    "output_multiplier": 1.5,
                    "cache_read_multiplier": 0.25,
                    "price_version": 3,
                }
            },
            version=3,
        )
        registry._initialized = False

        resolved = registry.resolve_public_model("gemini-fast", "responses")

        self.assertEqual(resolved.public_model.price_input_per_million, 20)
        self.assertEqual(resolved.public_model.price_output_per_million, 120)
        self.assertEqual(resolved.public_model.model_multiplier, 2)
        self.assertEqual(resolved.public_model.output_multiplier, 1.5)
        self.assertEqual(resolved.public_model.cache_read_multiplier, 0.25)
        self.assertEqual(resolved.public_model.price_version, 3)
        self.assertEqual(resolved.public_model.provider_model, "gemini-2.5-flash")

    def test_runtime_alias_override_changes_upstream_without_editing_catalog(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as override_file:
            json.dump(
                {
                    "aliases": {
                        "gemini-fast": {
                            "provider_model": "gemini-2.5-pro",
                            "upstream_model": "vertex-gemini-2.5-pro",
                        }
                    }
                },
                override_file,
            )
            override_file.flush()
            settings.model_alias_overrides_path = override_file.name
            registry._initialized = False
            registry.init_from_settings()

            resolved = registry.resolve_public_model("gemini-fast", "responses")

        self.assertEqual(resolved.public_model.public_id, "gemini-fast")
        self.assertEqual(resolved.public_model.provider_model, "gemini-2.5-pro")
        self.assertEqual(resolved.backend.model_id, "vertex-gemini-2.5-pro")

    def test_claude_compat_alias_switches_to_kiro_go_lane(self) -> None:
        settings.claude_compat_provider = "kiro_go"
        registry._initialized = False
        registry.init_from_settings()

        resolved = registry.resolve_public_model("claude-opus-4-8", "responses")

        self.assertEqual(resolved.public_model.delivery_lane, "kiro_go")
        self.assertEqual(resolved.backend.model_id, "gpt-5.4")
        self.assertEqual(resolved.backend.upstream_url, "https://kiro-go.example")
        self.assertEqual(resolved.backend.api_key, "kiro-key")
        self.assertEqual(resolved.backend.auth_style, "bearer")
        self.assertEqual(resolved.route_reason, "catalog:claude-opus-4-8:kiro_go")

    def test_claude_opus_short_alias_keeps_existing_kiro_go_model(self) -> None:
        catalog = json.loads(settings.model_catalog_json)
        catalog["models"].append(
            {
                "id": "opus",
                "owned_by": "coincoin",
                "provider_name": "",
                "provider_model": "gpt-5.4",
                "capabilities": ["chat/completions", "responses"],
                "routing_mode": "direct",
                "delivery_lane": "upstream_direct",
                "upstream_model": "gpt-5.4",
                "upstream_url": "https://legacy-claude.example/v1",
                "api_key": "legacy-claude-key",
                "auth_style": "bearer",
                "price_input_per_million": 500,
                "price_output_per_million": 2500,
                "billable_sku": "claude-code-compat-text",
                "metadata": {"compat_family": "claude-code"},
            }
        )
        settings.model_catalog_json = json.dumps(catalog)
        settings.claude_compat_provider = "kiro_go"
        registry._initialized = False
        registry.init_from_settings()

        resolved = registry.resolve_public_model("opus", "responses")

        self.assertEqual(resolved.public_model.delivery_lane, "kiro_go")
        self.assertEqual(resolved.backend.model_id, "claude-opus-4.7")
        self.assertEqual(resolved.route_reason, "catalog:opus:kiro_go")

    def test_explicit_kiro_public_model_aliases_resolve_when_enabled(self) -> None:
        settings.model_catalog_json = json.dumps(
            {
                "default_text_model": "claude-opus-4.7",
                "models": [
                    {
                        "id": "claude-opus-4.7",
                        "owned_by": "coincoin",
                        "provider_name": "",
                        "provider_model": "claude-opus-4.7",
                        "capabilities": ["chat/completions", "responses"],
                        "routing_mode": "direct",
                        "delivery_lane": "upstream_direct",
                        "upstream_model": "claude-opus-4.7",
                        "upstream_url": "https://legacy-claude.example/v1",
                        "api_key": "legacy-claude-key",
                        "auth_style": "bearer",
                        "price_input_per_million": 500,
                        "price_output_per_million": 2500,
                        "billable_sku": "claude-code-compat-text",
                        "metadata": {"compat_family": "claude-code"},
                    },
                    {
                        "id": "claude-sonnet-4.5",
                        "owned_by": "coincoin",
                        "provider_name": "",
                        "provider_model": "claude-sonnet-4.5",
                        "capabilities": ["chat/completions", "responses"],
                        "routing_mode": "direct",
                        "delivery_lane": "upstream_direct",
                        "upstream_model": "claude-sonnet-4.5",
                        "upstream_url": "https://legacy-claude.example/v1",
                        "api_key": "legacy-claude-key",
                        "auth_style": "bearer",
                        "price_input_per_million": 300,
                        "price_output_per_million": 1500,
                        "billable_sku": "claude-code-compat-text",
                        "metadata": {"compat_family": "claude-code"},
                    },
                    {
                        "id": "claude-haiku-4.5",
                        "owned_by": "coincoin",
                        "provider_name": "",
                        "provider_model": "claude-haiku-4.5",
                        "capabilities": ["chat/completions", "responses"],
                        "routing_mode": "direct",
                        "delivery_lane": "upstream_direct",
                        "upstream_model": "claude-haiku-4.5",
                        "upstream_url": "https://legacy-claude.example/v1",
                        "api_key": "legacy-claude-key",
                        "auth_style": "bearer",
                        "price_input_per_million": 100,
                        "price_output_per_million": 500,
                        "billable_sku": "claude-code-compat-text",
                        "metadata": {"compat_family": "claude-code"},
                    },
                ],
            }
        )
        settings.claude_compat_provider = "kiro_go"
        registry._initialized = False
        registry.init_from_settings()

        opus = registry.resolve_public_model("claude-opus-4.7", "responses")
        sonnet = registry.resolve_public_model("claude-sonnet-4.5", "responses")
        haiku = registry.resolve_public_model("claude-haiku-4.5", "responses")

        self.assertEqual(opus.public_model.delivery_lane, "kiro_go")
        self.assertEqual(opus.backend.model_id, "claude-opus-4.7")
        self.assertEqual(sonnet.backend.model_id, "claude-sonnet-4.5")
        self.assertEqual(haiku.backend.model_id, "claude-haiku-4.5")

    def test_runtime_alias_override_snapshot_is_used_from_memory(self) -> None:
        settings.model_alias_overrides_path = "/tmp/coincoin-override-file-should-not-be-read.json"
        registry.set_runtime_alias_overrides(
            {
                "gemini-fast": {
                    "provider_model": "gemini-2.5-pro",
                    "upstream_model": "vertex-gemini-2.5-pro",
                }
            },
            version=42,
        )
        registry._initialized = False

        with patch("app.router._load_json_file", side_effect=AssertionError("hot path should not read override file")):
            registry.init_from_settings()
            resolved = registry.resolve_public_model("gemini-fast", "responses")

        self.assertEqual(resolved.public_model.provider_model, "gemini-2.5-pro")
        self.assertEqual(resolved.backend.model_id, "vertex-gemini-2.5-pro")

    def test_db_alias_override_enabled_only_preserves_catalog_models(self) -> None:
        overrides, version = override_rows_to_snapshot(
            [
                type(
                    "Row",
                    (),
                    {
                        "alias_id": "gemini-fast",
                        "provider_model": "",
                        "upstream_model": "",
                        "enabled": 1,
                        "updated_at": None,
                    },
                )()
            ]
        )
        registry.set_runtime_alias_overrides(overrides, version=version)
        registry._initialized = False
        registry.init_from_settings()

        resolved = registry.resolve_public_model("gemini-fast", "responses")

        self.assertEqual(resolved.public_model.provider_model, "gemini-2.5-flash")
        self.assertEqual(resolved.backend.model_id, "gemini-2.5-flash")

    def test_explicit_legacy_public_model_keeps_legacy_lane(self) -> None:
        resolved = registry.resolve_public_model(
            "gpt-5.2-codex",
            "chat/completions",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
        )

        self.assertEqual(resolved.public_model.public_id, "gpt-5.2-codex")
        self.assertEqual(resolved.public_model.provider_model, "gpt-5.3-codex")
        self.assertEqual(resolved.backend.model_id, "gpt-5.3-codex")
        self.assertEqual(resolved.execution_profile, "legacy_coding")
        self.assertEqual(resolved.execution_pool, "cpa_coding_pool")
        self.assertEqual(resolved.route_reason, "catalog:gpt-5.2-codex:legacy_explicit")
        self.assertTrue(resolved.lock_model_selection)

    def test_fixed_model_gpt_5_2_codex_maps_to_cpa_supported_codex_model(self) -> None:
        settings.fixed_model = "gpt-5.2-codex"
        registry._initialized = False
        registry.init_from_settings()

        resolved = registry.resolve_public_model(
            "gpt-5.2-codex",
            "chat/completions",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
        )

        self.assertEqual(resolved.public_model.public_id, "gpt-5.2-codex")
        self.assertEqual(resolved.public_model.provider_model, "gpt-5.3-codex")
        self.assertEqual(resolved.backend.model_id, "gpt-5.3-codex")
        self.assertEqual(resolved.execution_profile, "legacy_coding")
        self.assertEqual(resolved.execution_pool, "cpa_coding_pool")

    def test_fixed_model_gpt_5_6_maps_to_sol_provider_model(self) -> None:
        settings.fixed_model = "gpt-5.6"
        registry._initialized = False
        registry.init_from_settings()

        resolved = registry.resolve_public_model(
            "gpt-5.6",
            "chat/completions",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
        )

        self.assertEqual(resolved.public_model.public_id, "gpt-5.6")
        self.assertEqual(resolved.public_model.provider_model, "gpt-5.6-sol")
        self.assertEqual(resolved.backend.model_id, "gpt-5.6-sol")

    def test_gpt_5_6_uses_official_cache_creation_multiplier(self) -> None:
        resolved = registry.resolve_public_model(
            "gpt-5.6-terra",
            "responses",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
        )

        self.assertEqual(resolved.public_model.price_input_per_million, 250)
        self.assertEqual(resolved.public_model.cache_creation_multiplier, 1.25)
        self.assertEqual(resolved.public_model.effective_cache_creation_input_per_million, 312.5)

    def test_checked_in_grok_models_are_route_only_and_share_grok_4_5_upstream(self) -> None:
        settings.model_catalog_json = ""
        registry._initialized = False
        registry.init_from_settings()

        grok = registry.get_public_model("grok-4.5")
        build = registry.get_public_model("grok-build")

        self.assertIsNotNone(grok)
        self.assertIsNotNone(build)
        for model in (grok, build):
            self.assertEqual(model.owned_by, "xai")
            self.assertEqual(model.provider_model, "grok-4.5")
            self.assertEqual(model.upstream_model, "grok-4.5")
            self.assertEqual(model.routing_mode, "route_only")
            self.assertEqual(model.delivery_lane, "route_only")
            self.assertEqual(model.capabilities, ("chat/completions", "responses"))
            self.assertEqual(
                model.metadata["supported_parameters"],
                ["stream", "reasoning_effort", "tools", "vision"],
            )

        self.assertEqual(build.billable_sku, "xai-grok-build-text")
        self.assertEqual(build.metadata["preferred_api_backend"], "responses")
        with self.assertRaises(ModelCapabilityError):
            registry.resolve_public_model("grok-build", "responses")

    def test_explicit_gpt_5_2_alias_keeps_legacy_lane(self) -> None:
        resolved = registry.resolve_public_model(
            "gpt-5.2",
            "responses",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
        )

        self.assertEqual(resolved.public_model.public_id, "gpt-5.2")
        self.assertEqual(resolved.backend.model_id, "gpt-5.2")
        self.assertEqual(resolved.execution_profile, "legacy_general")
        self.assertEqual(resolved.execution_pool, "cpa_general_pool")
        self.assertEqual(resolved.route_reason, "catalog:gpt-5.2:legacy_explicit")
        self.assertTrue(resolved.lock_model_selection)

    def test_explicit_gpt_5_5_alias_keeps_legacy_lane(self) -> None:
        resolved = registry.resolve_public_model(
            "gpt-5.5",
            "responses",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
        )

        self.assertEqual(resolved.public_model.public_id, "gpt-5.5")
        self.assertEqual(resolved.backend.model_id, "gpt-5.5")
        self.assertEqual(resolved.execution_profile, "legacy_general")
        self.assertEqual(resolved.execution_pool, "cpa_general_pool")
        self.assertEqual(resolved.route_reason, "catalog:gpt-5.5:legacy_explicit")
        self.assertTrue(resolved.lock_model_selection)

    def test_explicit_gpt_5_4_alias_routes_to_real_gpt_5_4_when_fixed_model_changes(self) -> None:
        settings.fixed_model = "gpt-5.5"
        registry._initialized = False
        registry.init_from_settings()

        resolved = registry.resolve_public_model(
            "gpt-5.4",
            "responses",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
        )

        self.assertEqual(resolved.public_model.public_id, "gpt-5.4")
        self.assertEqual(resolved.backend.model_id, "gpt-5.4")
        self.assertEqual(resolved.route_reason, "catalog:gpt-5.4:legacy_explicit")
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
        self.assertEqual(resolved.execution_profile, "legacy_general")
        self.assertEqual(resolved.execution_pool, "cpa_general_pool")
        self.assertEqual(resolved.route_reason, "catalog:gpt-5.4-mini:legacy_explicit")
        self.assertTrue(resolved.lock_model_selection)

    def test_gpt_5_3_codex_uses_coding_profile_and_cpa_coding_pool(self) -> None:
        resolved = registry.resolve_public_model(
            "gpt-5.3-codex",
            "responses",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
        )

        self.assertEqual(resolved.public_model.public_id, "gpt-5.3-codex")
        self.assertEqual(resolved.execution_profile, "legacy_coding")
        self.assertEqual(resolved.execution_pool, "cpa_coding_pool")
        self.assertEqual(resolved.backend.model_id, "gpt-5.3-codex")
        self.assertEqual(resolved.route_reason, "catalog:gpt-5.3-codex:legacy_explicit")
        self.assertTrue(resolved.lock_model_selection)

    def test_gpt_5_3_codex_spark_uses_coding_profile_and_admin_route_candidates(self) -> None:
        resolved = registry.resolve_public_model(
            "gpt-5.3-codex-spark",
            "responses",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
        )

        self.assertEqual(resolved.public_model.public_id, "gpt-5.3-codex-spark")
        self.assertEqual(resolved.public_model.provider_model, "gpt-5.3-codex-spark")
        self.assertEqual(resolved.public_model.metadata["display_name"], "GPT 5.3 Codex Spark")
        self.assertEqual(resolved.public_model.metadata["context_length"], 128000)
        self.assertEqual(resolved.execution_profile, "legacy_coding")
        self.assertEqual(resolved.execution_pool, "cpa_coding_pool")
        self.assertEqual(resolved.backend.model_id, "gpt-5.3-codex-spark")
        self.assertEqual(resolved.route_reason, "catalog:gpt-5.3-codex-spark:legacy_explicit")
        self.assertTrue(resolved.lock_model_selection)

        candidate_ids = {item["id"] for item in registry.candidate_alias_targets("gpt-5.3-codex")}
        self.assertIn("gpt-5.3-codex-spark", candidate_ids)

    def test_codex_auto_review_uses_cpa_model_id_and_coding_profile(self) -> None:
        resolved = registry.resolve_public_model(
            "codex-auto-review",
            "responses",
            messages=[{"role": "user", "content": "hello"}],
            tools=[{"type": "function", "function": {"name": "approve", "parameters": {}}}],
        )

        self.assertEqual(resolved.public_model.public_id, "codex-auto-review")
        self.assertEqual(resolved.public_model.provider_model, "codex-auto-review")
        self.assertEqual(resolved.public_model.created, 1776902400)
        self.assertEqual(resolved.public_model.metadata["supported_parameters"], ["tools"])
        self.assertEqual(resolved.public_model.metadata["thinking"]["levels"], ["low", "medium", "high", "xhigh"])
        self.assertEqual(resolved.execution_profile, "legacy_coding")
        self.assertEqual(resolved.execution_pool, "cpa_coding_pool")
        self.assertEqual(resolved.backend.model_id, "codex-auto-review")
        self.assertEqual(resolved.route_reason, "catalog:codex-auto-review:legacy_explicit")
        self.assertTrue(resolved.lock_model_selection)

    def test_catalog_lists_all_expected_legacy_gpt_aliases(self) -> None:
        text_model_ids = [
            model.public_id
            for model in registry.list_public_models("chat/completions")
            if model.routing_mode == "legacy_auto"
        ]

        self.assertEqual(text_model_ids, LEGACY_PUBLIC_TEXT_MODELS)

    def test_legacy_public_aliases_expose_default_text_prices(self) -> None:
        model = registry.get_public_model("gpt-5.4")

        self.assertIsNotNone(model)
        self.assertEqual(model.price_input_per_million, 250)
        self.assertEqual(model.price_output_per_million, 1500)

    def test_default_image_model_is_used_when_model_is_omitted(self) -> None:
        resolved = registry.resolve_public_model(None, "images/generations")

        self.assertEqual(resolved.public_model.public_id, "gpt-image-2")
        self.assertEqual(resolved.public_model.delivery_lane, "upstream_direct")
        self.assertEqual(resolved.backend.model_id, "gpt-image-2")
        self.assertEqual(resolved.backend.upstream_url, "https://fallback.example/v1")
        self.assertEqual(resolved.execution_profile, "upstream_direct_direct")
        self.assertEqual(resolved.execution_pool, "upstream_direct_direct_pool")
        self.assertEqual(resolved.route_reason, "catalog:gpt-image-2:upstream_direct")

    def test_default_image_model_supports_image_edits(self) -> None:
        resolved = registry.resolve_public_model(None, "images/edits")

        self.assertEqual(resolved.public_model.public_id, "gpt-image-2")
        self.assertEqual(resolved.public_model.delivery_lane, "upstream_direct")
        self.assertEqual(resolved.backend.model_id, "gpt-image-2")
        self.assertEqual(resolved.backend.upstream_url, "https://fallback.example/v1")
        self.assertEqual(resolved.execution_profile, "upstream_direct_direct")
        self.assertEqual(resolved.execution_pool, "upstream_direct_direct_pool")
        self.assertEqual(resolved.route_reason, "catalog:gpt-image-2:upstream_direct")

    def test_explicit_gemini_image_model_uses_native_cpa_route(self) -> None:
        resolved = registry.resolve_public_model("gemini-image", "images/generations")

        self.assertEqual(resolved.public_model.public_id, "gemini-image")
        self.assertEqual(resolved.public_model.delivery_lane, "cpa_gemini")
        self.assertEqual(resolved.backend.model_id, "gemini-3.1-flash-image")
        self.assertEqual(resolved.backend.upstream_url, "https://gemini-cpa.example/v1")
        self.assertEqual(resolved.execution_profile, "cpa_gemini_direct")
        self.assertEqual(resolved.execution_pool, "cpa_gemini_direct_pool")
        self.assertEqual(resolved.route_reason, "catalog:gemini-image:cpa_gemini")

    def test_string_false_enabled_flag_hides_public_gemini_models(self) -> None:
        catalog = json.loads(settings.model_catalog_json)
        for model in catalog["models"]:
            if model.get("owned_by") == "google":
                model["enabled"] = "false"
        settings.model_catalog_json = json.dumps(catalog)
        registry._initialized = False
        registry.init_from_settings()

        public_ids = [model.public_id for model in registry.list_public_models()]
        self.assertNotIn("gemini-fast", public_ids)
        self.assertNotIn("gemini-image", public_ids)

        resolved = registry.resolve_public_model(None, "images/generations")
        self.assertEqual(resolved.public_model.public_id, "gpt-image-2")

    def test_openai_image_lane_can_become_default_when_gemini_is_disabled(self) -> None:
        catalog = json.loads(settings.model_catalog_json)
        for model in catalog["models"]:
            if model.get("owned_by") == "google":
                model["enabled"] = "false"
        settings.model_catalog_json = json.dumps(catalog)
        registry._initialized = False
        registry.init_from_settings()

        resolved = registry.resolve_public_model(None, "images/generations")

        self.assertEqual(resolved.public_model.public_id, "gpt-image-2")
        self.assertEqual(resolved.public_model.delivery_lane, "upstream_direct")
        self.assertEqual(resolved.backend.model_id, "gpt-image-2")
        self.assertEqual(resolved.backend.upstream_url, "https://fallback.example/v1")
        self.assertEqual(resolved.backend.auth_style, "azure")
        self.assertEqual(resolved.execution_profile, "upstream_direct_direct")
        self.assertEqual(resolved.execution_pool, "upstream_direct_direct_pool")
        self.assertEqual(resolved.route_reason, "catalog:gpt-image-2:upstream_direct")

    def test_default_embedding_model_uses_dedicated_azure_lane(self) -> None:
        resolved = registry.resolve_public_model(None, "embeddings")

        self.assertEqual(resolved.public_model.public_id, "text-embedding-3-small")
        self.assertEqual(resolved.public_model.delivery_lane, "upstream_direct")
        self.assertEqual(resolved.backend.model_id, "text-embedding-3-small")
        self.assertEqual(resolved.backend.upstream_url, "https://fallback.example/v1")
        self.assertEqual(resolved.backend.auth_style, "azure")
        self.assertEqual(resolved.execution_profile, "embedding_direct")
        self.assertEqual(resolved.execution_pool, "upstream_embedding_pool")
        self.assertEqual(resolved.route_reason, "catalog:text-embedding-3-small:upstream_direct")

    def test_default_video_model_uses_seedance_lane(self) -> None:
        resolved = registry.resolve_public_model(None, "videos/generations")

        self.assertEqual(resolved.public_model.public_id, "seedance-v2-720p")
        self.assertEqual(resolved.public_model.delivery_lane, "upstream_direct")
        self.assertEqual(resolved.public_model.price_per_video_cents, 98)
        self.assertEqual(resolved.backend.model_id, "seedance-v2-720p")
        self.assertEqual(resolved.backend.upstream_url, "https://api.wgspai.cn")
        self.assertEqual(resolved.backend.auth_style, "bearer")
        self.assertEqual(resolved.execution_profile, "upstream_direct_direct")
        self.assertEqual(resolved.execution_pool, "upstream_direct_direct_pool")
        self.assertEqual(resolved.route_reason, "catalog:seedance-v2-720p:upstream_direct")

    def test_explicit_embedding_model_uses_dedicated_azure_lane(self) -> None:
        resolved = registry.resolve_public_model("text-embedding-3-small", "embeddings")

        self.assertEqual(resolved.public_model.public_id, "text-embedding-3-small")
        self.assertEqual(resolved.backend.model_id, "text-embedding-3-small")
        self.assertEqual(resolved.backend.upstream_url, "https://fallback.example/v1")
        self.assertEqual(resolved.backend.auth_style, "azure")
        self.assertEqual(resolved.execution_profile, "embedding_direct")
        self.assertEqual(resolved.execution_pool, "upstream_embedding_pool")
        self.assertEqual(resolved.route_reason, "catalog:text-embedding-3-small:upstream_direct")

    def test_nested_placeholder_prefers_explicit_image_upstream_url(self) -> None:
        template = "${COINCOIN_IMAGE_UPSTREAM_URL:-${COINCOIN_FALLBACK_UPSTREAM_URL:-${COINCOIN_UPSTREAM_BASE_URL}}}"
        with patch.dict(
            os.environ,
            {
                "COINCOIN_IMAGE_UPSTREAM_URL": "https://cliproxyapi-deploy-production.up.railway.app/v1",
                "COINCOIN_FALLBACK_UPSTREAM_URL": "https://fallback.example/v1",
                "COINCOIN_UPSTREAM_BASE_URL": "https://legacy.example/v1",
            },
            clear=False,
        ):
            resolved = _resolve_placeholders(template)

        self.assertEqual(resolved, "https://cliproxyapi-deploy-production.up.railway.app/v1")

    def test_nested_placeholder_falls_back_cleanly_without_trailing_braces(self) -> None:
        template = "${COINCOIN_IMAGE_UPSTREAM_URL:-${COINCOIN_FALLBACK_UPSTREAM_URL:-${COINCOIN_UPSTREAM_BASE_URL}}}"
        with patch.dict(
            os.environ,
            {
                "COINCOIN_IMAGE_UPSTREAM_URL": "",
                "COINCOIN_FALLBACK_UPSTREAM_URL": "https://fallback.example/v1",
                "COINCOIN_UPSTREAM_BASE_URL": "https://legacy.example/v1",
            },
            clear=False,
        ):
            resolved = _resolve_placeholders(template)

        self.assertEqual(resolved, "https://fallback.example/v1")

    def test_image_model_rejects_chat_endpoint(self) -> None:
        with self.assertRaises(ModelCapabilityError):
            registry.resolve_public_model("gemini-image", "chat/completions")

    def test_video_model_rejects_chat_endpoint(self) -> None:
        with self.assertRaises(ModelCapabilityError):
            registry.resolve_public_model("seedance-v2-720p", "chat/completions")

    def test_legacy_text_model_rejects_embeddings_endpoint(self) -> None:
        with self.assertRaises(ModelCapabilityError):
            registry.resolve_public_model("gpt-5.2-codex", "embeddings")


if __name__ == "__main__":
    unittest.main()
