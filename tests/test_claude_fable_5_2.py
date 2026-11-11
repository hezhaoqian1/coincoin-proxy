import os
import unittest

os.environ.setdefault("COINCOIN_DATABASE_URL", "mysql://test@127.0.0.1:3306/test")

from app.channel_router import ModelChannelRouteSnapshot, ProviderChannelSnapshot, channel_router
from app.config import settings
from app.router import ModelCapabilityError, registry
from app.usage_buffer import calculate_token_cost, extract_total_input_tokens


class ClaudeFable52CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        settings.model_catalog_json = ""
        registry.set_runtime_pricing_overrides({}, version=1)
        registry._initialized = False
        registry.init_from_settings()
        self.fable = registry.get_public_model("claude-fable-5-2")

    def tearDown(self) -> None:
        channel_router.clear_snapshot()
        registry.clear_runtime_pricing_overrides()
        registry._initialized = False

    def test_checked_in_entry_uses_native_route_and_fourfold_pricing(self) -> None:
        fable = self.fable
        self.assertIsNotNone(fable)
        self.assertEqual(fable.owned_by, "anthropic")
        self.assertEqual(fable.provider_model, "claude-fable-5-2")
        self.assertEqual(fable.upstream_model, "claude-fable-5-2")
        self.assertEqual(fable.routing_mode, "route_only")
        self.assertEqual(fable.delivery_lane, "route_only")
        self.assertEqual(fable.capabilities, ("chat/completions",))
        self.assertEqual(fable.auth_style, "x-api-key")
        self.assertEqual(fable.billable_sku, "claude-fable-5-2-text")
        # Official base price $8 / $40; every Claude public model retails at 4x.
        self.assertEqual(fable.base_price_input_per_million, 800)
        self.assertEqual(fable.base_price_output_per_million, 4000)
        self.assertEqual(fable.model_multiplier, 4.0)
        self.assertEqual(fable.output_multiplier, 1.0)
        self.assertEqual(fable.price_input_per_million, 3200)
        self.assertEqual(fable.price_output_per_million, 16000)
        self.assertEqual(fable.cache_read_multiplier, 0.025)
        self.assertEqual(fable.cache_creation_multiplier, 1.25)
        # 4x official cache read $0.20 and 5m cache write $10 per million tokens.
        self.assertEqual(fable.effective_cached_input_per_million, 80.0)
        self.assertEqual(fable.effective_cache_creation_input_per_million, 4000.0)
        self.assertEqual(fable.context_pricing_tiers, ())
        self.assertEqual(fable.metadata["provider_protocol"], "anthropic_messages")
        self.assertEqual(fable.metadata["context_length"], 1_000_000)
        self.assertEqual(fable.metadata["max_completion_tokens"], 128_000)
        self.assertTrue(fable.metadata["thinking"]["always_on"])
        self.assertEqual(fable.metadata["thinking"]["default_effort"], "high")

    def test_older_fable_base_prices_are_untouched(self) -> None:
        # Fable 5 / 5.1 keep their own $10 / $50 base price variables; their 4x
        # retail multiplier comes from the runtime /admin/model-pricing override.
        for model_id in ("claude-fable-5-1", "claude-fable-5"):
            with self.subTest(model=model_id):
                old = registry.get_public_model(model_id)
                self.assertIsNotNone(old)
                self.assertEqual(old.base_price_input_per_million, 1000)
                self.assertEqual(old.base_price_output_per_million, 5000)

    def test_requires_active_anthropic_route(self) -> None:
        for endpoint in ("chat/completions", "responses"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ModelCapabilityError):
                registry.resolve_public_model("claude-fable-5-2", endpoint)

    def test_resolves_through_anthropic_channel_route(self) -> None:
        channel_router.set_snapshot(
            [
                ProviderChannelSnapshot(
                    channel_id="ch_anthropic",
                    provider_platform="anthropic",
                    channel_type="anthropic_compatible",
                    base_url="https://anthropic-relay.example",
                    api_key="anthropic-key",
                    auth_style="x-api-key",
                    priority=0,
                    capabilities=("chat/completions",),
                )
            ],
            [
                ModelChannelRouteSnapshot(
                    route_id="mcr_fable_5_2",
                    public_model_id="claude-fable-5-2",
                    endpoint="chat/completions",
                    channel_id="ch_anthropic",
                    upstream_model="claude-fable-5-2",
                    transform_profile="anthropic_messages",
                )
            ],
        )

        resolved = registry.resolve_public_model("claude-fable-5-2", "chat/completions")
        self.assertEqual(resolved.backend.channel_id, "ch_anthropic")
        self.assertEqual(resolved.backend.channel_type, "anthropic_compatible")
        self.assertEqual(resolved.backend.transform_profile, "anthropic_messages")
        self.assertEqual(resolved.backend.model_id, "claude-fable-5-2")
        self.assertEqual(resolved.backend.auth_style, "x-api-key")
        self.assertEqual(resolved.route_reason, "catalog:claude-fable-5-2:route_only:channel:ch_anthropic")
        # A 5.2 route must not make 5.1 routable.
        with self.assertRaises(ModelCapabilityError):
            registry.resolve_public_model("claude-fable-5-1", "chat/completions")

    def test_cost_uses_fable_52_token_and_cache_prices(self) -> None:
        fable = self.fable
        usage = {"input_tokens": 300_000, "cache_read_input_tokens": 600_000, "cache_creation_input_tokens": 50_000}
        cents, details = calculate_token_cost(
            extract_total_input_tokens(usage),
            20_000,
            cached_tokens=usage["cache_read_input_tokens"],
            cache_creation_tokens=usage["cache_creation_input_tokens"],
            price_input_per_million=fable.price_input_per_million,
            price_output_per_million=fable.price_output_per_million,
            cached_price_input_per_million=fable.effective_cached_input_per_million,
            cache_creation_price_input_per_million=fable.effective_cache_creation_input_per_million,
            context_pricing_tiers=fable.context_pricing_tiers,
        )
        self.assertEqual(details["tier"], "standard")
        # Retail 4x card: 300k*3200 + 600k*80 + 50k*4000 + 20k*16000 (cents per million)
        self.assertAlmostEqual(cents, (300_000 * 3200 + 600_000 * 80 + 50_000 * 4000 + 20_000 * 16000) / 1_000_000)


if __name__ == "__main__":
    unittest.main()
