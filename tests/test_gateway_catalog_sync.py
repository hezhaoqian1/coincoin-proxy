import json
import unittest
from pathlib import Path

import yaml

IMAGE_CAPABILITIES = {"images/generations", "images/edits"}
EMBEDDING_CAPABILITIES = {"embeddings"}
TEXT_CAPABILITIES = {"chat/completions", "responses"}


class GatewayCatalogSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        catalog_path = repo_root / "coincoin-proxy" / "config" / "model_catalog.json"
        gateway_path = repo_root / "services" / "litellm-gateway" / "config.yaml"

        cls.catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        cls.gateway = yaml.safe_load(gateway_path.read_text(encoding="utf-8"))
        cls.gateway_aliases = {
            item["model_name"]: item.get("litellm_params") or {}
            for item in (cls.gateway.get("model_list") or [])
            if isinstance(item, dict) and item.get("model_name")
        }
        cls.direct_models = [
            item
            for item in (cls.catalog.get("models") or [])
            if isinstance(item, dict) and item.get("routing_mode") == "direct"
        ]
        cls.gateway_models = [
            item
            for item in cls.direct_models
            if item.get("delivery_lane") == "gateway"
        ]
        cls.upstream_direct_models = [
            item
            for item in cls.direct_models
            if item.get("delivery_lane") == "upstream_direct"
        ]

    def test_every_gateway_public_model_targets_a_gateway_alias(self) -> None:
        missing_aliases = sorted(
            model["upstream_model"]
            for model in self.gateway_models
            if model.get("upstream_model") not in self.gateway_aliases
        )
        self.assertEqual(missing_aliases, [])

    def test_direct_public_models_only_use_supported_delivery_lanes(self) -> None:
        invalid_models = sorted(
            model["id"]
            for model in self.direct_models
            if model.get("delivery_lane") not in {"gateway", "upstream_direct"}
        )
        self.assertEqual(invalid_models, [])

    def test_default_models_exist_and_match_capabilities(self) -> None:
        public_models = {
            item["id"]: item
            for item in (self.catalog.get("models") or [])
            if isinstance(item, dict) and item.get("id")
        }

        default_text_model = self.catalog.get("default_text_model")
        default_image_model = self.catalog.get("default_image_model")

        self.assertIn(default_text_model, public_models)
        self.assertIn("chat/completions", public_models[default_text_model].get("capabilities") or [])
        self.assertIn(default_image_model, public_models)
        self.assertIn("images/generations", public_models[default_image_model].get("capabilities") or [])

    def test_text_models_match_gemini_gateway_shape(self) -> None:
        for model in self.gateway_models:
            capabilities = set(model.get("capabilities") or [])
            if capabilities.intersection(IMAGE_CAPABILITIES):
                continue

            alias_name = model["upstream_model"]
            litellm_params = self.gateway_aliases[alias_name]

            with self.subTest(model=model["id"]):
                self.assertEqual(
                    litellm_params.get("model"),
                    f"gemini/{model['provider_model']}",
                )
                self.assertEqual(
                    litellm_params.get("api_base"),
                    "os.environ/VERTEX_GEMINI_API_BASE",
                )
                self.assertEqual(
                    litellm_params.get("api_key"),
                    "os.environ/VERTEX_API_KEY",
                )

    def test_image_models_match_vertex_image_gateway_shape(self) -> None:
        for model in self.gateway_models:
            capabilities = set(model.get("capabilities") or [])
            if not capabilities.intersection(IMAGE_CAPABILITIES):
                continue

            alias_name = model["upstream_model"]
            litellm_params = self.gateway_aliases[alias_name]

            with self.subTest(model=model["id"]):
                self.assertEqual(
                    litellm_params.get("model"),
                    f"gemini/{model['provider_model']}",
                )
                self.assertEqual(
                    litellm_params.get("api_base"),
                    "os.environ/VERTEX_GEMINI_API_BASE",
                )
                self.assertEqual(
                    litellm_params.get("api_key"),
                    "os.environ/VERTEX_API_KEY",
                )

    def test_upstream_direct_models_use_azure_openai_shape(self) -> None:
        for model in self.upstream_direct_models:
            capabilities = set(model.get("capabilities") or [])
            with self.subTest(model=model["id"]):
                if capabilities.intersection(EMBEDDING_CAPABILITIES):
                    self.assertEqual(capabilities, EMBEDDING_CAPABILITIES)
                    self.assertEqual(model.get("provider_name"), "OpenAI")
                    self.assertEqual(model.get("upstream_model"), model.get("provider_model"))
                    self.assertEqual(
                        model.get("upstream_url"),
                        "${COINCOIN_EMBEDDING_UPSTREAM_URL:-${COINCOIN_FALLBACK_UPSTREAM_URL:-${COINCOIN_UPSTREAM_BASE_URL}}}",
                    )
                    self.assertEqual(
                        model.get("api_key"),
                        "${COINCOIN_EMBEDDING_API_KEY:-${COINCOIN_FALLBACK_API_KEY:-${COINCOIN_UPSTREAM_API_KEY}}}",
                    )
                    continue

                if capabilities.intersection(IMAGE_CAPABILITIES):
                    self.assertEqual(model.get("provider_name"), "OpenAI")
                    self.assertEqual(model.get("upstream_model"), model.get("provider_model"))
                    self.assertEqual(
                        model.get("upstream_url"),
                        "${COINCOIN_IMAGE_UPSTREAM_URL:-${COINCOIN_FALLBACK_UPSTREAM_URL:-${COINCOIN_UPSTREAM_BASE_URL}}}",
                    )
                    self.assertEqual(
                        model.get("api_key"),
                        "${COINCOIN_IMAGE_API_KEY:-${COINCOIN_FALLBACK_API_KEY:-${COINCOIN_UPSTREAM_API_KEY}}}",
                    )
                    continue

                if capabilities.intersection(TEXT_CAPABILITIES):
                    self.assertEqual(model.get("upstream_model"), model.get("provider_model"))
                    self.assertEqual(
                        model.get("upstream_url"),
                        "${COINCOIN_UPSTREAM_BASE_URL}",
                    )
                    self.assertEqual(
                        model.get("api_key"),
                        "${COINCOIN_UPSTREAM_API_KEY}",
                    )
                    self.assertEqual(
                        model.get("auth_style"),
                        "${COINCOIN_PRIMARY_AUTH_STYLE:-azure}",
                    )
                    continue

                self.fail(f"unexpected upstream_direct capability set for {model['id']}: {sorted(capabilities)}")


if __name__ == "__main__":
    unittest.main()
