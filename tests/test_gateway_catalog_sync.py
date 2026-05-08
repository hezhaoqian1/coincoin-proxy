import json
import unittest
from pathlib import Path

import yaml

IMAGE_CAPABILITIES = {"images/generations", "images/edits"}
EMBEDDING_CAPABILITIES = {"embeddings"}
TEXT_CAPABILITIES = {"chat/completions", "responses"}
FIXED_TEXT_PRICE = (500, 3000)
CHEAP_TEXT_PRICE = (75, 450)
CLAUDE_OPUS_PRICE = (500, 2500)
CLAUDE_SONNET_PRICE = (300, 1500)
CLAUDE_HAIKU_PRICE = (100, 500)
FIXED_INPUT_PRICE_PLACEHOLDER = "${COINCOIN_PRICE_INPUT_PER_MILLION:-500}"
FIXED_OUTPUT_PRICE_PLACEHOLDER = "${COINCOIN_PRICE_OUTPUT_PER_MILLION:-3000}"
CHEAP_INPUT_PRICE_PLACEHOLDER = "${COINCOIN_CHEAP_PRICE_INPUT:-${COINCOIN_GPT_54_MINI_INPUT_PRICE:-75}}"
CHEAP_OUTPUT_PRICE_PLACEHOLDER = "${COINCOIN_CHEAP_PRICE_OUTPUT:-${COINCOIN_GPT_54_MINI_OUTPUT_PRICE:-450}}"
CLAUDE_OPUS_INPUT_PRICE_PLACEHOLDER = "${COINCOIN_CLAUDE_OPUS_INPUT_PRICE:-500}"
CLAUDE_OPUS_OUTPUT_PRICE_PLACEHOLDER = "${COINCOIN_CLAUDE_OPUS_OUTPUT_PRICE:-2500}"
CLAUDE_SONNET_INPUT_PRICE_PLACEHOLDER = "${COINCOIN_CLAUDE_SONNET_INPUT_PRICE:-300}"
CLAUDE_SONNET_OUTPUT_PRICE_PLACEHOLDER = "${COINCOIN_CLAUDE_SONNET_OUTPUT_PRICE:-1500}"
CLAUDE_HAIKU_INPUT_PRICE_PLACEHOLDER = "${COINCOIN_CLAUDE_HAIKU_INPUT_PRICE:-100}"
CLAUDE_HAIKU_OUTPUT_PRICE_PLACEHOLDER = "${COINCOIN_CLAUDE_HAIKU_OUTPUT_PRICE:-500}"
CLAUDE_OPUS_ALIASES = {
    "claude-opus-4-7",
    "opus",
    "best",
    "default",
    "opus[1m]",
    "opusplan",
}
CLAUDE_SONNET_ALIASES = {
    "claude-sonnet-4-6",
    "sonnet",
    "sonnet[1m]",
}
CLAUDE_HAIKU_ALIASES = {
    "claude-haiku-4-5",
    "claude-haiku-4-5-20251001",
    "haiku",
}
OFFICIAL_DEFAULT_TEXT_PRICES = {
    "${COINCOIN_FIXED_MODEL}": FIXED_TEXT_PRICE,
    "gpt-5.4": (250, 1500),
    "gpt-5": (125, 1000),
    "gpt-5.1": (125, 1000),
    "gpt-5.1-codex": (125, 1000),
    "gpt-5.1-codex-mini": CHEAP_TEXT_PRICE,
    "gpt-5.1-codex-max": FIXED_TEXT_PRICE,
    "gpt-5.2": (175, 1400),
    "gpt-5.2-codex": (175, 1400),
    "gpt-5.3-codex": (175, 1400),
    "gpt-5.4-mini": CHEAP_TEXT_PRICE,
    "gpt-5.5": FIXED_TEXT_PRICE,
    "claude-opus-4-7": CLAUDE_OPUS_PRICE,
    "claude-sonnet-4-6": CLAUDE_SONNET_PRICE,
    "claude-haiku-4-5": CLAUDE_HAIKU_PRICE,
    "claude-haiku-4-5-20251001": CLAUDE_HAIKU_PRICE,
    "opus": CLAUDE_OPUS_PRICE,
    "sonnet": CLAUDE_SONNET_PRICE,
    "haiku": CLAUDE_HAIKU_PRICE,
    "best": CLAUDE_OPUS_PRICE,
    "default": CLAUDE_OPUS_PRICE,
    "opus[1m]": CLAUDE_OPUS_PRICE,
    "sonnet[1m]": CLAUDE_SONNET_PRICE,
    "opusplan": CLAUDE_OPUS_PRICE,
    "${COINCOIN_EMBEDDING_MODEL:-text-embedding-3-small}": (2, 0),
    "gemini-balanced": (10, 40),
    "gemini-fast": (30, 250),
    "gemini-reasoning": (125, 1000),
    "vertex-gemini-2.5-flash-lite": (10, 40),
    "vertex-gemini-2.5-flash": (30, 250),
    "vertex-gemini-2.5-pro": (125, 1000),
    "vertex-gemini-3.1-flash-lite-preview": (10, 40),
    "vertex-gemini-3-flash-preview": (50, 300),
    "vertex-gemini-3.1-pro-preview": (125, 1000),
}
OFFICIAL_DEFAULT_IMAGE_PRICES = {
    "${COINCOIN_IMAGE_MODEL:-gpt-image-1}": 4.0,
    "gemini-image": 3.9,
    "vertex-gemini-2.5-flash-image": 3.9,
    "vertex-gemini-3.1-flash-image-preview": 3.9,
}


def _placeholder_default(value):
    if not isinstance(value, str):
        return value
    marker = ":-"
    if marker not in value or not value.endswith("}"):
        return value
    default = value.rsplit(marker, 1)[1][:-1]
    while isinstance(default, str) and default.endswith("}") and default.count("${") < default.count("}"):
        default = default[:-1]
    return default


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

    def test_legacy_gpt_5_4_stays_public_when_fixed_model_changes(self) -> None:
        public_models = {
            item["id"]: item
            for item in (self.catalog.get("models") or [])
            if isinstance(item, dict) and item.get("id")
        }

        model = public_models["gpt-5.4"]
        self.assertEqual(model.get("provider_model"), "gpt-5.4")
        self.assertEqual(model.get("routing_mode"), "legacy_auto")
        self.assertIn("chat/completions", model.get("capabilities") or [])
        self.assertIn("responses", model.get("capabilities") or [])

    def test_claude_compat_aliases_use_official_claude_price_defaults(self) -> None:
        public_models = {
            item["id"]: item
            for item in (self.catalog.get("models") or [])
            if isinstance(item, dict) and item.get("id")
        }

        for alias in CLAUDE_OPUS_ALIASES:
            with self.subTest(alias=alias):
                model = public_models[alias]
                self.assertEqual(model.get("provider_model"), "${COINCOIN_FIXED_MODEL}")
                self.assertEqual(model.get("upstream_model"), "${COINCOIN_FIXED_MODEL}")
                self.assertEqual(model.get("price_input_per_million"), CLAUDE_OPUS_INPUT_PRICE_PLACEHOLDER)
                self.assertEqual(model.get("price_output_per_million"), CLAUDE_OPUS_OUTPUT_PRICE_PLACEHOLDER)

        for alias in CLAUDE_SONNET_ALIASES:
            with self.subTest(alias=alias):
                model = public_models[alias]
                expected = "${COINCOIN_CHEAP_MODEL:-${COINCOIN_FIXED_MODEL}}"
                self.assertEqual(model.get("provider_model"), expected)
                self.assertEqual(model.get("upstream_model"), expected)
                self.assertEqual(model.get("price_input_per_million"), CLAUDE_SONNET_INPUT_PRICE_PLACEHOLDER)
                self.assertEqual(model.get("price_output_per_million"), CLAUDE_SONNET_OUTPUT_PRICE_PLACEHOLDER)

        for alias in CLAUDE_HAIKU_ALIASES:
            with self.subTest(alias=alias):
                model = public_models[alias]
                expected = "${COINCOIN_CHEAP_MODEL:-${COINCOIN_FIXED_MODEL}}"
                self.assertEqual(model.get("provider_model"), expected)
                self.assertEqual(model.get("upstream_model"), expected)
                self.assertEqual(model.get("price_input_per_million"), CLAUDE_HAIKU_INPUT_PRICE_PLACEHOLDER)
                self.assertEqual(model.get("price_output_per_million"), CLAUDE_HAIKU_OUTPUT_PRICE_PLACEHOLDER)

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
                    compat_family = ((model.get("metadata") or {}).get("compat_family") if isinstance(model.get("metadata"), dict) else None)
                    expected_auth_style = "bearer" if compat_family == "claude-code" else "${COINCOIN_PRIMARY_AUTH_STYLE:-azure}"
                    self.assertEqual(model.get("auth_style"), expected_auth_style)
                    continue

                self.fail(f"unexpected upstream_direct capability set for {model['id']}: {sorted(capabilities)}")

    def test_billable_public_models_have_official_default_prices(self) -> None:
        public_models = {
            item["id"]: item
            for item in (self.catalog.get("models") or [])
            if isinstance(item, dict) and item.get("id")
        }

        for model_id, expected_prices in OFFICIAL_DEFAULT_TEXT_PRICES.items():
            with self.subTest(model=model_id):
                model = public_models[model_id]
                actual = (
                    int(_placeholder_default(model.get("price_input_per_million"))),
                    int(_placeholder_default(model.get("price_output_per_million"))),
                )
                self.assertEqual(actual, expected_prices)

        for model_id, expected_price in OFFICIAL_DEFAULT_IMAGE_PRICES.items():
            with self.subTest(model=model_id):
                model = public_models[model_id]
                actual = float(_placeholder_default(model.get("price_per_image_cents")))
                self.assertEqual(actual, expected_price)

        zero_default_fields = []
        for model in public_models.values():
            capabilities = set(model.get("capabilities") or [])
            for field in ("price_input_per_million", "price_output_per_million", "price_per_image_cents"):
                if field == "price_output_per_million" and capabilities == EMBEDDING_CAPABILITIES:
                    continue
                if field == "price_per_image_cents" and not capabilities.intersection(IMAGE_CAPABILITIES):
                    continue
                if field in {"price_input_per_million", "price_output_per_million"} and capabilities.intersection(IMAGE_CAPABILITIES):
                    continue
                if _placeholder_default(model.get(field)) == "0":
                    zero_default_fields.append(f"{model['id']}:{field}")
        self.assertEqual(zero_default_fields, [])


if __name__ == "__main__":
    unittest.main()
