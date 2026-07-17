import asyncio
import unittest
from datetime import UTC, date, datetime

from app.config import settings
from app.usage_buffer import (
    UsageBuffer,
    china_today,
    extract_cache_creation_tokens,
    extract_cache_read_tokens,
    extract_cached_tokens,
    extract_server_side_tool_usage_details,
    extract_total_input_tokens,
    total_server_side_tools_used,
)


class UsageBufferUnitsTests(unittest.TestCase):
    def test_china_today_rolls_over_at_beijing_midnight(self) -> None:
        self.assertEqual(china_today(datetime(2026, 5, 2, 15, 59, 59, tzinfo=UTC)), date(2026, 5, 2))
        self.assertEqual(china_today(datetime(2026, 5, 2, 16, 0, 0, tzinfo=UTC)), date(2026, 5, 3))

    def test_extract_cached_tokens_accepts_anthropic_cache_read_shape(self) -> None:
        self.assertEqual(extract_cached_tokens({"cache_read_input_tokens": 1234}), 1234)

    def test_extract_cache_tokens_accepts_openai_and_anthropic_shapes(self) -> None:
        self.assertEqual(
            extract_cache_read_tokens({"prompt_tokens_details": {"cached_tokens": 28491}}),
            28491,
        )
        self.assertEqual(
            extract_cache_read_tokens({"input_tokens_details": {"cached_tokens": 8000}}),
            8000,
        )
        self.assertEqual(
            extract_cache_creation_tokens(
                {
                    "cache_creation_input_tokens": 300,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 100,
                        "ephemeral_1h_input_tokens": 200,
                    },
                }
            ),
            300,
        )
        self.assertEqual(
            extract_total_input_tokens(
                {
                    "input_tokens": 50,
                    "cache_read_input_tokens": 100_000,
                    "cache_creation_input_tokens": 0,
                }
            ),
            100_050,
        )
        self.assertEqual(
            extract_total_input_tokens(
                {"input_tokens": 2006, "input_tokens_details": {"cached_tokens": 1920}}
            ),
            2006,
        )
        self.assertEqual(
            extract_cache_creation_tokens(
                {
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 100,
                        "ephemeral_1h_input_tokens": 200,
                    },
                }
            ),
            300,
        )

    def test_extract_server_side_tool_usage_details_is_bounded_and_normalized(self) -> None:
        details = extract_server_side_tool_usage_details(
            {
                "num_server_side_tools_used": 99,
                "server_side_tool_usage_details": {
                    "web_search_calls": "4",
                    "x_search_calls": 1,
                    "code_interpreter_calls": -2,
                    "file_search_calls": None,
                    "mcp_calls": 2.8,
                    "unknown_provider_counter": 50,
                },
            }
        )

        self.assertEqual(
            details,
            {
                "web_search_calls": 4,
                "x_search_calls": 1,
                "code_interpreter_calls": 0,
                "file_search_calls": 0,
                "mcp_calls": 2,
                "document_search_calls": 0,
                "image_generation_calls": 0,
            },
        )
        self.assertEqual(total_server_side_tools_used(details), 7)

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
        self.assertEqual(request_logs[0]["cache_read_tokens"], 500_000)
        self.assertEqual(request_logs[0]["cache_creation_tokens"], 0)

    def test_cached_tokens_can_use_model_specific_effective_price(self) -> None:
        original_rate = settings.cache_discount_rate
        settings.cache_discount_rate = 0.1
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        buffer = UsageBuffer()

        async def scenario():
            await buffer.add(
                "u_cached_override",
                input_tokens=1_000_000,
                output_tokens=0,
                cached_tokens=500_000,
                requests=1,
                endpoint="responses",
                model="priced-fast",
                customer_model_alias="priced-fast",
                provider_model="gemini-2.5-flash",
                usage_unit_type="tokens",
                billable_sku="priced-fast-text",
                price_input_per_million=100,
                price_output_per_million=0,
                effective_cached_input_per_million=25,
            )
            return await buffer.snapshot_and_reset()

        try:
            _, usage_by_user, request_logs = loop.run_until_complete(scenario())
        finally:
            settings.cache_discount_rate = original_rate
            asyncio.set_event_loop(None)
            loop.close()

        self.assertAlmostEqual(usage_by_user["u_cached_override"]["cost_cents_f"], 62.5)
        self.assertEqual(request_logs[0]["effective_cached_input_per_million"], 25)

    def test_cache_creation_tokens_are_tracked_without_extra_charge(self) -> None:
        original_rate = settings.cache_discount_rate
        settings.cache_discount_rate = 0.1
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        buffer = UsageBuffer()

        async def scenario():
            await buffer.add(
                "u_cache_write",
                input_tokens=1_000_000,
                output_tokens=0,
                cache_read_tokens=500_000,
                cache_creation_tokens=100_000,
                requests=1,
                api_key_id="k_cache_write",
                endpoint="messages",
                model="claude-opus-4-7",
                customer_model_alias="claude-opus-4-7",
                provider_model="gpt-5.5",
                usage_unit_type="tokens",
                billable_sku="claude-code-compat-text",
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

        self.assertEqual(round(usage_by_user["u_cache_write"]["cost_cents_f"]), 55)
        self.assertEqual(request_logs[0]["cached_tokens"], 500_000)
        self.assertEqual(request_logs[0]["cache_read_tokens"], 500_000)
        self.assertEqual(request_logs[0]["cache_creation_tokens"], 100_000)

    def test_cache_creation_tokens_can_use_model_specific_effective_price(self) -> None:
        original_rate = settings.cache_discount_rate
        settings.cache_discount_rate = 0.1
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        buffer = UsageBuffer()

        async def scenario():
            await buffer.add(
                "u_gpt56_cache_write",
                input_tokens=1_000_000,
                output_tokens=0,
                cache_read_tokens=500_000,
                cache_creation_tokens=100_000,
                requests=1,
                api_key_id="k_gpt56_cache_write",
                endpoint="responses",
                model="gpt-5.6",
                customer_model_alias="gpt-5.6",
                provider_model="gpt-5.6-sol",
                usage_unit_type="tokens",
                billable_sku="legacy-gpt-5.6-sol-text",
                price_input_per_million=100,
                price_output_per_million=0,
                effective_cached_input_per_million=10,
                effective_cache_creation_input_per_million=125,
            )
            return await buffer.snapshot_and_reset()

        try:
            _, usage_by_user, request_logs = loop.run_until_complete(scenario())
        finally:
            settings.cache_discount_rate = original_rate
            asyncio.set_event_loop(None)
            loop.close()

        self.assertAlmostEqual(usage_by_user["u_gpt56_cache_write"]["cost_cents_f"], 57.5)
        self.assertEqual(request_logs[0]["cache_creation_tokens"], 100_000)

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
                provider_model="gemini-3.1-flash-image",
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
        self.assertEqual(request_logs[0]["provider_model"], "gemini-3.1-flash-image")

    def test_tracks_video_generation_units_and_cost(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        buffer = UsageBuffer()

        async def scenario():
            await buffer.add(
                "u_video",
                requests=1,
                endpoint="videos/generations",
                model="seedance-v2-720p",
                customer_model_alias="seedance-v2-720p",
                provider_model="seedance-v2-720p",
                usage_unit_type="videos",
                usage_unit_count=2,
                video_count=2,
                price_per_video_cents=98,
                base_price_per_video_cents=98,
                video_multiplier=1,
                billable_sku="seedance-v2-720p-video-task",
            )
            return await buffer.snapshot_and_reset()

        try:
            daily, usage_by_user, request_logs = loop.run_until_complete(scenario())
        finally:
            asyncio.set_event_loop(None)
            loop.close()

        self.assertEqual(daily[("u_video", china_today())]["videos_total"], 2)
        self.assertEqual(round(usage_by_user["u_video"]["cost_cents_f"]), 196)
        self.assertEqual(request_logs[0]["usage_unit_type"], "videos")
        self.assertEqual(request_logs[0]["usage_unit_count"], 2)
        self.assertEqual(request_logs[0]["video_count"], 2)
        self.assertEqual(request_logs[0]["price_per_video_cents"], 98)
        self.assertEqual(request_logs[0]["base_price_per_video_cents"], 98)
        self.assertEqual(request_logs[0]["video_multiplier"], 1)

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
                provider_model="gemini-3.1-flash-image",
                usage_unit_type="images",
                usage_unit_count=3,
                image_count=3,
                price_per_image_cents=6.7,
                billable_sku="gemini-image",
            )
            return await buffer.snapshot_and_reset()

        try:
            _, usage_by_user, request_logs = loop.run_until_complete(scenario())
        finally:
            asyncio.set_event_loop(None)
            loop.close()

        self.assertAlmostEqual(usage_by_user["u_image_fractional"]["cost_cents_f"], 20.1)
        self.assertEqual(request_logs[0]["cost_cents"], 20)

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

    def test_records_pricing_snapshot_for_multiplier_audit(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        buffer = UsageBuffer()

        async def scenario():
            await buffer.add(
                "u_pricing",
                input_tokens=1_000_000,
                output_tokens=1_000_000,
                requests=1,
                endpoint="responses",
                model="priced-fast",
                customer_model_alias="priced-fast",
                provider_model="gemini-2.5-flash",
                usage_unit_type="tokens",
                billable_sku="priced-fast-text",
                price_input_per_million=150,
                price_output_per_million=600,
                pricing_mode="multiplier",
                model_multiplier=1.5,
                output_multiplier=2,
                cache_read_multiplier=0.2,
                image_multiplier=1,
                video_multiplier=1,
                base_price_input_per_million=100,
                base_price_output_per_million=200,
                base_price_per_video_cents=98,
                effective_cached_input_per_million=30,
                price_version=7,
            )
            return await buffer.snapshot_and_reset()

        try:
            _, usage_by_user, request_logs = loop.run_until_complete(scenario())
        finally:
            asyncio.set_event_loop(None)
            loop.close()

        self.assertEqual(round(usage_by_user["u_pricing"]["cost_cents_f"]), 750)
        self.assertEqual(request_logs[0]["pricing_mode"], "multiplier")
        self.assertEqual(request_logs[0]["model_multiplier"], 1.5)
        self.assertEqual(request_logs[0]["output_multiplier"], 2)
        self.assertEqual(request_logs[0]["cache_read_multiplier"], 0.2)
        self.assertEqual(request_logs[0]["video_multiplier"], 1)
        self.assertEqual(request_logs[0]["base_price_input_per_million"], 100)
        self.assertEqual(request_logs[0]["base_price_output_per_million"], 200)
        self.assertEqual(request_logs[0]["base_price_per_video_cents"], 98)
        self.assertEqual(request_logs[0]["effective_cached_input_per_million"], 30)
        self.assertEqual(request_logs[0]["price_version"], 7)

    def test_records_normalized_server_side_tool_usage_on_request_log(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        buffer = UsageBuffer()

        async def scenario():
            await buffer.add(
                "u_grok_search",
                input_tokens=100,
                output_tokens=20,
                requests=1,
                endpoint="responses",
                model="grok-build",
                server_side_tool_usage_details={
                    "web_search_calls": 4,
                    "x_search_calls": 1,
                    "unknown_provider_counter": 9,
                },
            )
            return await buffer.snapshot_and_reset()

        try:
            _, _, request_logs = loop.run_until_complete(scenario())
        finally:
            asyncio.set_event_loop(None)
            loop.close()

        self.assertEqual(
            request_logs[0]["server_side_tool_usage_details"],
            {
                "web_search_calls": 4,
                "x_search_calls": 1,
                "code_interpreter_calls": 0,
                "file_search_calls": 0,
                "mcp_calls": 0,
                "document_search_calls": 0,
                "image_generation_calls": 0,
            },
        )


if __name__ == "__main__":
    unittest.main()
