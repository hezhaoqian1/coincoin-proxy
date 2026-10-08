import os
import unittest

os.environ.setdefault("COINCOIN_DATABASE_URL", "mysql://test@127.0.0.1:3306/test")

from app.config import settings
from app.router import ModelCapabilityError, registry
from app.token_pricing import ContextPricingTier
from app.usage_buffer import calculate_token_cost, extract_total_input_tokens


class ClaudeHaiku55CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        settings.model_catalog_json = ""
        registry.set_runtime_pricing_overrides({}, version=1)
        registry._initialized = False
        registry.init_from_settings()
        self.haiku = registry.get_public_model("claude-haiku-5-5")

    def test_checked_in_entry_uses_native_route_and_fourfold_pricing(self) -> None:
        haiku = self.haiku
        self.assertIsNotNone(haiku)
        self.assertEqual(haiku.owned_by, "anthropic")
        self.assertEqual(haiku.provider_model, "claude-haiku-5-5")
        self.assertEqual(haiku.upstream_model, "claude-haiku-5-5")
        self.assertEqual(haiku.routing_mode, "route_only")
        self.assertEqual(haiku.delivery_lane, "route_only")
        self.assertEqual(haiku.capabilities, ("chat/completions",))
        self.assertEqual(haiku.auth_style, "x-api-key")
        self.assertEqual(haiku.billable_sku, "claude-haiku-5-5-text")
        self.assertEqual(haiku.base_price_input_per_million, 10)
        self.assertEqual(haiku.base_price_output_per_million, 50)
        self.assertEqual(haiku.model_multiplier, 4.0)
        self.assertEqual(haiku.output_multiplier, 1.0)
        self.assertEqual(haiku.price_input_per_million, 40)
        self.assertEqual(haiku.price_output_per_million, 200)
        self.assertEqual(haiku.cache_read_multiplier, 0.1)
        self.assertEqual(haiku.cache_creation_multiplier, 1.25)
        self.assertEqual(haiku.effective_cached_input_per_million, 4.0)
        self.assertEqual(haiku.effective_cache_creation_input_per_million, 50.0)
        self.assertEqual(haiku.context_pricing_tiers, (ContextPricingTier(100_000, 5.0, 5.0),))
        self.assertEqual(haiku.metadata["provider_protocol"], "anthropic_messages")
        self.assertEqual(haiku.metadata["context_length"], 1_000_000)
        self.assertEqual(haiku.metadata["max_completion_tokens"], 128_000)
        self.assertEqual(haiku.metadata["thinking"]["default_effort"], "medium")
        for endpoint in ("chat/completions", "responses"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ModelCapabilityError):
                registry.resolve_public_model("claude-haiku-5-5", endpoint)

    def test_older_haiku_is_untouched(self) -> None:
        old = registry.get_public_model("claude-haiku-4-5")
        self.assertIsNotNone(old)
        self.assertEqual(old.context_pricing_tiers, ())

    def test_prompt_over_100k_bills_whole_request_at_5x(self) -> None:
        haiku = self.haiku

        def cost(anthropic_usage, output_tokens=0):
            total_input = extract_total_input_tokens(anthropic_usage)
            return calculate_token_cost(
                total_input, output_tokens,
                cached_tokens=anthropic_usage.get("cache_read_input_tokens", 0),
                cache_creation_tokens=anthropic_usage.get("cache_creation_input_tokens", 0),
                price_input_per_million=haiku.price_input_per_million,
                price_output_per_million=haiku.price_output_per_million,
                cached_price_input_per_million=haiku.effective_cached_input_per_million,
                cache_creation_price_input_per_million=haiku.effective_cache_creation_input_per_million,
                context_pricing_tiers=haiku.context_pricing_tiers,
            )

        # Anthropic input_tokens excludes cache; 100,000 total prompt stays standard.
        usage = {"input_tokens": 70_000, "cache_read_input_tokens": 20_000, "cache_creation_input_tokens": 10_000}
        cents, details = cost(usage, output_tokens=10_000)
        self.assertEqual(details["tier"], "standard")
        # Retail 4x card: 70k*40 + 20k*4 + 10k*50 + 10k*200 (cents/M)
        self.assertAlmostEqual(cents, (70_000 * 40 + 20_000 * 4 + 10_000 * 50 + 10_000 * 200) / 1_000_000)

        usage = {"input_tokens": 70_001, "cache_read_input_tokens": 20_000, "cache_creation_input_tokens": 10_000}
        cents, details = cost(usage, output_tokens=10_000)
        self.assertEqual(details["tier"], "long_context")
        self.assertEqual(details["input_per_million_cents"], 200)
        self.assertEqual(details["cache_read_per_million_cents"], 20)
        self.assertEqual(details["cache_write_per_million_cents"], 250)
        self.assertEqual(details["output_per_million_cents"], 1000)
        self.assertAlmostEqual(cents, (70_001 * 200 + 20_000 * 20 + 10_000 * 250 + 10_000 * 1000) / 1_000_000)


if __name__ == "__main__":
    unittest.main()
