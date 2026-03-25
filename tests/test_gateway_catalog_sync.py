import json
import unittest
from pathlib import Path

import yaml

IMAGE_CAPABILITIES = {"images/generations", "images/edits"}


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

    def test_every_direct_public_model_targets_a_gateway_alias(self) -> None:
        missing_aliases = sorted(
            model["upstream_model"]
            for model in self.direct_models
            if model.get("upstream_model") not in self.gateway_aliases
        )
        self.assertEqual(missing_aliases, [])

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
        for model in self.direct_models:
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
        for model in self.direct_models:
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


if __name__ == "__main__":
    unittest.main()
