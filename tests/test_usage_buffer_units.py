import asyncio
import unittest
from datetime import UTC, date, datetime

from app.config import settings
from app.usage_buffer import UsageBuffer, china_today, extract_cached_tokens


class UsageBufferUnitsTests(unittest.TestCase):
    def test_china_today_rolls_over_at_beijing_midnight(self) -> None:
        self.assertEqual(china_today(datetime(2026, 5, 2, 15, 59, 59, tzinfo=UTC)), date(2026, 5, 2))
        self.assertEqual(china_today(datetime(2026, 5, 2, 16, 0, 0, tzinfo=UTC)), date(2026, 5, 3))

    def test_extract_cached_tokens_accepts_anthropic_cache_read_shape(self) -> None:
        self.assertEqual(extract_cached_tokens({"cache_read_input_tokens": 1234}), 1234)

    def test_cached_tokens_follow_configured_discount_rate(self) -> None:
        original_rate = settings.cache_discount_rate
        settings.cache_discount_rate = 0.1
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        buffer = UsageBuffer()

        async def scenario():
            await buffer.add(
                "u_cached",
                input_tokens=1_000_000,
                output_tokens=0,
                cached_tokens=500_000,
                requests=1,
                api_key_id="k_cached",
                endpoint="responses",
                model="gpt-5.4",
                customer_model_alias="gpt-5.4",
                provider_model="gpt-4o-mini",
                usage_unit_type="tokens",
                billable_sku="legacy-default-text",
                price_input_per_million=100,
                price_output_per_million=0,
            )
            return await buffer.snapshot_and_reset()

        try:
            _, usage_by_user, request_logs = loop.run_until_complete(scenario())
        finally:
            settings.cache_discount_rate = original_rate
            asyncio.set_event_loop(None)
            loop.close()

        self.assertEqual(round(usage_by_user["u_cached"]["cost_cents_f"]), 55)
        self.assertEqual(request_logs[0]["api_key_id"], "k_cached")
        self.assertEqual(request_logs[0]["cached_tokens"], 500_000)

    def test_tracks_image_generation_units_and_cost(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        buffer = UsageBuffer()

        async def scenario():
            await buffer.add(
                "u_image",
                requests=1,
                endpoint="images/generations",
                model="gemini-image",
                customer_model_alias="gemini-image",
                provider_model="gemini-3.1-flash-image-preview",
                usage_unit_type="images",
                usage_unit_count=2,
                image_count=2,
                price_per_image_cents=7,
                billable_sku="gemini-image",
            )
            return await buffer.snapshot_and_reset()

        try:
            daily, usage_by_user, request_logs = loop.run_until_complete(scenario())
        finally:
            asyncio.set_event_loop(None)
            loop.close()

        self.assertEqual(daily[("u_image", china_today())]["images_total"], 2)
        self.assertEqual(round(usage_by_user["u_image"]["cost_cents_f"]), 14)
        self.assertEqual(request_logs[0]["usage_unit_type"], "images")
        self.assertEqual(request_logs[0]["usage_unit_count"], 2)
        self.assertEqual(request_logs[0]["provider_model"], "gemini-3.1-flash-image-preview")

    def test_image_cost_keeps_sub_cent_official_prices_until_flush_rounding(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        buffer = UsageBuffer()

        async def scenario():
            await buffer.add(
                "u_image_fractional",
                requests=1,
                endpoint="images/generations",
                model="gemini-image",
                customer_model_alias="gemini-image",
                provider_model="gemini-3.1-flash-image-preview",
                usage_unit_type="images",
                usage_unit_count=3,
                image_count=3,
                price_per_image_cents=3.9,
                billable_sku="gemini-image",
            )
            return await buffer.snapshot_and_reset()

        try:
            _, usage_by_user, request_logs = loop.run_until_complete(scenario())
        finally:
            asyncio.set_event_loop(None)
            loop.close()

        self.assertAlmostEqual(usage_by_user["u_image_fractional"]["cost_cents_f"], 11.7)
        self.assertEqual(request_logs[0]["cost_cents"], 12)

    def test_tracks_text_alias_and_provider_model_separately(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        buffer = UsageBuffer()

        async def scenario():
            await buffer.add(
                "u_text",
                input_tokens=1_000_000,
                output_tokens=500_000,
                requests=1,
                endpoint="responses",
                model="gemini-fast",
                customer_model_alias="gemini-fast",
                provider_model="gemini-2.5-flash",
                usage_unit_type="tokens",
                billable_sku="gemini-fast-text",
                price_input_per_million=100,
                price_output_per_million=200,
            )
            return await buffer.snapshot_and_reset()

        try:
            daily, usage_by_user, request_logs = loop.run_until_complete(scenario())
        finally:
            asyncio.set_event_loop(None)
            loop.close()

        self.assertEqual(daily[("u_text", china_today())]["input_tokens"], 1_000_000)
        self.assertEqual(daily[("u_text", china_today())]["output_tokens"], 500_000)
        self.assertEqual(round(usage_by_user["u_text"]["cost_cents_f"]), 200)
        self.assertEqual(request_logs[0]["customer_model_alias"], "gemini-fast")
        self.assertEqual(request_logs[0]["provider_model"], "gemini-2.5-flash")
        self.assertEqual(request_logs[0]["usage_unit_type"], "tokens")
        self.assertEqual(request_logs[0]["usage_unit_count"], 1_500_000)


if __name__ == "__main__":
    unittest.main()
