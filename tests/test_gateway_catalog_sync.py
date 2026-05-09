import json
import unittest
from pathlib import Path

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
    "gemini-image": 6.7,
    "gemini-3.1-flash-image": 6.7,
    "vertex-gemini-2.5-flash-image": 6.7,
    "vertex-gemini-3.1-flash-image-preview": 6.7,
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

        cls.catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
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
        cls.cpa_gemini_models = [
            item
            for item in cls.direct_models
            if item.get("delivery_lane") == "cpa_gemini"
        ]
        cls.upstream_direct_models = [
            item
            for item in cls.direct_models
            if item.get("delivery_lane") == "upstream_direct"
        ]

    def test_gateway_lane_is_not_used_for_public_gemini(self) -> None:
        google_gateway_aliases = sorted(
            model["id"]
            for model in self.gateway_models
            if model.get("owned_by") == "google" or model.get("provider_name") == "Google"
        )
        self.assertEqual(google_gateway_aliases, [])

    def test_direct_public_models_only_use_supported_delivery_lanes(self) -> None:
        invalid_models = sorted(
            model["id"]
            for model in self.direct_models
            if model.get("delivery_lane") not in {"gateway", "cpa_gemini", "upstream_direct"}
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

    def test_legacy_codex_models_stay_off_gateway_lane(self) -> None:
        public_models = {
            item["id"]: item
            for item in (self.catalog.get("models") or [])
            if isinstance(item, dict) and item.get("id")
        }

        for model_id in ("gpt-5.2-codex", "gpt-5.3-codex"):
            with self.subTest(model=model_id):
                model = public_models[model_id]
                metadata = model.get("metadata") or {}
                self.assertEqual(model.get("routing_mode"), "legacy_auto")
                self.assertEqual(model.get("delivery_lane"), "legacy")
                self.assertEqual(metadata.get("execution_profile"), "legacy_coding")
                self.assertEqual(metadata.get("execution_pool"), "cpa_coding_pool")

    def test_text_models_match_native_cpa_gemini_shape(self) -> None:
        for model in self.cpa_gemini_models:
            capabilities = set(model.get("capabilities") or [])
            if capabilities.intersection(IMAGE_CAPABILITIES):
                continue

            with self.subTest(model=model["id"]):
                self.assertEqual(model.get("provider_name"), "Google")
                self.assertEqual(model.get("upstream_model"), model.get("provider_model"))
                self.assertEqual(model.get("upstream_url"), "${COINCOIN_GEMINI_CPA_BASE_URL}/v1")
                self.assertEqual(model.get("api_key"), "${COINCOIN_GEMINI_CPA_API_KEY}")
                self.assertEqual(model.get("auth_style"), "${COINCOIN_GEMINI_CPA_AUTH_STYLE:-bearer}")
                metadata = model.get("metadata") or {}
                self.assertEqual(metadata.get("provider_platform"), "cpa_gemini")
                self.assertEqual(metadata.get("channel_id"), "gemini-cpa-primary")

    def test_image_models_match_native_cpa_gemini_shape(self) -> None:
        for model in self.cpa_gemini_models:
            capabilities = set(model.get("capabilities") or [])
            if not capabilities.intersection(IMAGE_CAPABILITIES):
                continue

            with self.subTest(model=model["id"]):
                self.assertEqual(model.get("provider_name"), "Google")
                self.assertEqual(model.get("upstream_model"), model.get("provider_model"))
                self.assertEqual(model.get("upstream_url"), "${COINCOIN_GEMINI_CPA_BASE_URL}/v1")
                self.assertEqual(model.get("api_key"), "${COINCOIN_GEMINI_CPA_API_KEY}")
                self.assertEqual(model.get("auth_style"), "${COINCOIN_GEMINI_CPA_AUTH_STYLE:-bearer}")
                metadata = model.get("metadata") or {}
                self.assertEqual(metadata.get("provider_platform"), "cpa_gemini")
                self.assertEqual(metadata.get("channel_id"), "gemini-cpa-primary")

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
