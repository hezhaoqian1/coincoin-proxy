import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("COINCOIN_DATABASE_URL", "mysql://test@127.0.0.1:3306/test")

from app.admin import _pricing_payload
from app.anthropic_adapter import openai_usage_from_anthropic_usage
from app.openai_compat import _chat_usage_payload_from_counts, _serialize_public_model
from app.proxy import _responses_usage_from_chat_usage
from app.router import registry
from app.station_runtime import StationResolvedModel, usage_pricing_kwargs
from app.stations import _serialize_station_model_alias
from app.token_pricing import (
    ContextPricingTier, GPT_LONG_CONTEXT_MODELS,
    parse_context_pricing_tiers,
)
from app.usage_buffer import (
    UsageBuffer, _request_log_insert_values, calculate_cost_cents,
    calculate_token_cost, extract_cache_creation_tokens, extract_cache_read_tokens,
    extract_total_input_tokens,
)
from app.usage_events import build_usage_event


GPT_TIERS = (ContextPricingTier(272_000, 2, 1.5),)


def build_model(model_id="gpt-6.1-sol", pricing=None):
    return registry._build_public_model({
        "id": model_id, "owned_by": "openai", "routing_mode": "legacy_auto",
        "provider_model": model_id, "price_input_per_million": 200,
        "price_output_per_million": 1000, "pricing": pricing or {},
    })


def gpt_cost(input_tokens, output_tokens=0, **kwargs):
    return calculate_token_cost(
        input_tokens, output_tokens, price_input_per_million=200,
        price_output_per_million=1000, cached_price_input_per_million=10,
        cache_creation_price_input_per_million=250,
        context_pricing_tiers=GPT_TIERS, **kwargs,
    )


class ContextCacheBillingTests(unittest.TestCase):
    def test_openai_cache_writes_are_subsets_not_additional_input(self):
        for input_key, details_key in (("input_tokens", "input_tokens_details"), ("prompt_tokens", "prompt_tokens_details")):
            with self.subTest(details=details_key):
                usage = {input_key: 100_000, details_key: {"cached_tokens": 20_000, "cache_write_tokens": 70_000}}
                self.assertEqual(extract_total_input_tokens(usage), 100_000)
                self.assertEqual(extract_cache_read_tokens(usage), 20_000)
                self.assertEqual(extract_cache_creation_tokens(usage), 70_000)

    def test_explicit_zero_cache_write_wins_over_compatibility_aliases(self):
        usage = {"input_tokens": 100, "input_tokens_details": {"cache_write_tokens": 0},
                 "cache_creation_input_tokens": 50, "cache_read_input_tokens": 20}
        self.assertEqual(extract_cache_creation_tokens(usage), 0)
        self.assertEqual(extract_total_input_tokens(usage), 100)
        self.assertEqual(extract_cache_creation_tokens({"cache_creation_input_tokens": 0,
            "cache_creation": {"ephemeral_5m_input_tokens": 50}}), 0)

    def test_anthropic_cache_semantics_and_compatibility_round_trip(self):
        usage = {"input_tokens": 10_000, "output_tokens": 100,
                 "cache_read_input_tokens": 200_000,
                 "cache_creation": {"ephemeral_5m_input_tokens": 50_000, "ephemeral_1h_input_tokens": 40_000}}
        self.assertEqual(extract_total_input_tokens(usage), 300_000)
        chat = openai_usage_from_anthropic_usage(usage)
        responses = _responses_usage_from_chat_usage(chat)
        self.assertEqual(extract_total_input_tokens(responses), 300_000)
        self.assertEqual(extract_cache_read_tokens(responses), 200_000)
        self.assertEqual(extract_cache_creation_tokens(responses), 90_000)

    def test_cache_write_only_survives_chat_stream_and_responses_conversion(self):
        chat = _chat_usage_payload_from_counts(100_000, 10, 0, 100_000)
        response = _responses_usage_from_chat_usage(chat)
        self.assertEqual(response["input_tokens_details"]["cache_write_tokens"], 100_000)
        self.assertEqual(response["input_tokens"], 100_000)

    def test_cache_write_reproduction_now_charges_twenty_five_cents(self):
        cost, prices = gpt_cost(100_000, cache_creation_tokens=100_000)
        self.assertEqual(cost, 25)
        self.assertEqual(prices["cache_write_per_million_cents"], 250)

    def test_threshold_is_strict_and_applies_to_the_entire_request(self):
        for tokens in (271_999, 272_000, 272_001):
            with self.subTest(input_tokens=tokens):
                cost, prices = gpt_cost(tokens, 10_000)
                is_long = tokens > 272_000
                self.assertEqual(prices["tier"], "long_context" if is_long else "standard")
                expected = tokens * (400 if is_long else 200) / 1_000_000 + (15 if is_long else 10)
                self.assertAlmostEqual(cost, expected)

    def test_long_context_reproduction_charges_one_dollar_thirty_five(self):
        cost, _ = gpt_cost(300_000, 10_000)
        self.assertEqual(cost, 135)

    def test_output_is_not_part_of_context_threshold(self):
        cost, prices = gpt_cost(270_000, 100_000)
        self.assertEqual(prices["tier"], "standard")
        self.assertEqual(cost, 154)

    def test_cached_input_counts_toward_threshold_and_all_components_change_price(self):
        cost, prices = gpt_cost(300_000, 10_000, cached_tokens=100_000, cache_creation_tokens=50_000)
        self.assertEqual(cost, 102)
        self.assertEqual(prices["cache_read_per_million_cents"], 20)
        self.assertEqual(prices["cache_write_per_million_cents"], 500)
        self.assertEqual(prices["output_per_million_cents"], 1500)

    def test_zero_prices_are_not_replaced_by_defaults(self):
        self.assertEqual(calculate_cost_cents(300_000, 10_000,
            cached_tokens=300_000, price_input_per_million=200, price_output_per_million=0,
            cached_price_input_per_million=0, context_pricing_tiers=GPT_TIERS), 0)

    def test_malformed_cache_counts_are_bounded(self):
        for value in (-5, None, "invalid", [], {}):
            with self.subTest(value=value):
                self.assertEqual(extract_cache_creation_tokens({"input_tokens_details": {"cache_write_tokens": value}}), 0)
        cost, _ = gpt_cost(100, cached_tokens=100, cache_creation_tokens=100)
        self.assertAlmostEqual(cost, 0.001)

    def test_multiple_tiers_select_highest_threshold_without_compounding(self):
        tiers = parse_context_pricing_tiers([
            {"above_input_tokens": 400_000, "input_multiplier": 3, "output_multiplier": 2},
            {"above_input_tokens": 272_000, "input_multiplier": 2, "output_multiplier": 1.5},
        ])
        cost, prices = calculate_token_cost(450_000, 10_000,
            price_input_per_million=200, price_output_per_million=1000, context_pricing_tiers=tiers)
        self.assertEqual(cost, 290)
        self.assertEqual(prices["above_input_tokens"], 400_000)

    def test_invalid_catalog_tiers_are_rejected(self):
        valid = {"above_input_tokens": 272_000, "input_multiplier": 2, "output_multiplier": 1.5}
        invalid = ["invalid", [{}], [valid, valid]]
        for key, values in (("above_input_tokens", [0, -1, True, 2.5]),
                            ("input_multiplier", [0, -1, float("nan"), float("inf"), True])):
            invalid.extend([{**valid, key: value}] for value in values)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_context_pricing_tiers(value)

    def test_exact_gpt_ids_have_tiers_but_unrelated_models_do_not(self):
        for model_id in GPT_LONG_CONTEXT_MODELS:
            with self.subTest(model=model_id):
                self.assertEqual(build_model(model_id).context_pricing_tiers, GPT_TIERS)
        for model_id in ("codex-auto-review", "gpt-image-2", "claude-opus-5", "my-gpt-6"):
            self.assertEqual(build_model(model_id).context_pricing_tiers, ())

    def test_catalog_can_disable_tiers_and_default_slot_gets_cache_policy(self):
        self.assertEqual(build_model(pricing={"context_pricing_tiers": []}).context_pricing_tiers, ())
        model = build_model("gpt-5.6-sol")
        self.assertEqual(model.cache_creation_multiplier, 1.25)
        self.assertEqual(build_model().cache_read_multiplier, 0.05)

    def test_public_and_admin_apis_publish_same_context_rule(self):
        model = build_model()
        public = _serialize_public_model(model)
        with patch.object(registry, "get_public_model", return_value=model):
            admin = _pricing_payload(model.public_id)
        self.assertEqual(public["coincoin_context_pricing_tiers"], admin["context_pricing_tiers"])
        self.assertEqual(public["coincoin_context_pricing_basis"], "whole_request")
        self.assertEqual(admin["effective_cache_creation_input_per_million"], 250)

    def test_station_catalog_publishes_target_tiers_and_retail_cache_rates(self):
        alias = SimpleNamespace(alias="fast", target_public_model_id="gpt-6.1-sol",
            capability="chat/completions", created_at=None, station_id="st_1",
            is_default_text=True, is_default_image=False)
        price = SimpleNamespace(retail_input_per_million_cents=400,
            retail_output_per_million_cents=2000, retail_price_per_image_cents=0,
            billable_sku="fast")
        for ratio in (0.05, 0):
            model = build_model(pricing={"cache_read_multiplier": ratio})
            with self.subTest(cache_read_multiplier=ratio), patch.object(registry, "get_public_model", return_value=model):
                payload = _serialize_station_model_alias(alias, price)
            self.assertEqual(payload["coincoin_price_cached_input_per_million"], 400 * ratio)
            self.assertEqual(payload["coincoin_price_cache_creation_input_per_million"], 500)
            self.assertEqual(payload["coincoin_context_pricing_tiers"], _serialize_public_model(model)["coincoin_context_pricing_tiers"])


class ContextCacheBillingBufferTests(unittest.IsolatedAsyncioTestCase):
    async def test_buffer_persists_exact_selected_rates_and_free_user_cache(self):
        model = build_model()
        buffer = UsageBuffer()
        await buffer.add("u_tier", input_tokens=300_000, output_tokens=10_000,
            cache_read_tokens=100_000, cache_creation_tokens=50_000,
            requests=1, endpoint="responses", model=model.public_id,
            price_input_per_million=200, price_output_per_million=1000,
            **usage_pricing_kwargs(model, user_cache_read_multiplier_override=0))
        _, users, logs = await buffer.snapshot_and_reset()
        self.assertEqual(users["u_tier"]["cost_cents_f"], 100)
        log = logs[0]
        self.assertEqual(log["pricing_details"]["tier"], "long_context")
        self.assertEqual(log["effective_cached_input_per_million"], 0)
        self.assertEqual(_request_log_insert_values(log)["pricing_details"], log["pricing_details"])
        self.assertEqual(build_usage_event(log).request_log["pricing_details"], log["pricing_details"])

    async def test_station_cache_prices_use_retail_rate_but_wholesale_uses_public_rate(self):
        model = build_model()
        station = StationResolvedModel(
            resolved_model=SimpleNamespace(public_model=model), display_model="fast",
            station_id="st_1", station_alias="fast", resolved_public_model=model.public_id,
            retail_input_per_million=400, retail_output_per_million=2000,
            retail_price_per_image_cents=0, wholesale_input_per_million=200,
            wholesale_output_per_million=1000, wholesale_price_per_image_cents=0, price_version=1,
        )
        buffer = UsageBuffer()
        await buffer.add("u_station", input_tokens=300_000, output_tokens=10_000,
            cache_read_tokens=100_000, cache_creation_tokens=50_000,
            requests=1, endpoint="responses", model="fast",
            price_input_per_million=400, price_output_per_million=2000,
            **usage_pricing_kwargs(model, station))
        _, _, logs = await buffer.snapshot_and_reset()
        self.assertEqual(logs[0]["cost_cents"], 204)
        self.assertEqual(logs[0]["wholesale_cost_cents"], 102)
        self.assertEqual(logs[0]["pricing_details"]["cache_read_per_million_cents"], 40)

    async def test_no_context_rule_means_no_implicit_tier_from_backend_name(self):
        buffer = UsageBuffer()
        await buffer.add("u_custom", input_tokens=300_000, output_tokens=10_000,
            provider_model="gpt-6.1-sol", model="custom-price", price_input_per_million=200,
            price_output_per_million=1000, requests=1)
        _, _, logs = await buffer.snapshot_and_reset()
        self.assertEqual(logs[0]["cost_cents"], 70)
        self.assertEqual(logs[0]["pricing_details"]["tier"], "standard")


if __name__ == "__main__":
    unittest.main()
