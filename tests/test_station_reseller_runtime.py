import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

import app.station_runtime as station_runtime
from app.usage_buffer import UsageBuffer


class _ScalarOneOrNoneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, execute_results=None):
        self.execute_results = list(execute_results or [])

    async def execute(self, _query):
        if not self.execute_results:
            raise AssertionError("unexpected execute call")
        return self.execute_results.pop(0)


class StationResellerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_station_alias_resolution_uses_platform_target_and_retail_price(self):
        station_context = {
            "station_id": "st_1",
            "slug": "stone",
            "status": "active",
            "mode": "commission_station",
            "default_text_alias": "fast",
            "default_image_alias": "",
        }
        user = SimpleNamespace(id="u_customer", _station_context=station_context)
        alias = SimpleNamespace(
            id="sa_1",
            station_id="st_1",
            alias="fast",
            target_public_model_id="gpt-5.4-mini",
            fallback_target_public_model_id="",
            capability="chat/completions",
            status="active",
        )
        price = SimpleNamespace(
            id="sp_1",
            station_alias_id="sa_1",
            billable_sku="legacy-gpt-5.4-mini-text",
            usage_unit_type="tokens",
            retail_input_per_million_cents=120,
            retail_output_per_million_cents=720,
            retail_price_per_image_cents=0.0,
            price_version=3,
            status="active",
        )
        public_model = SimpleNamespace(
            public_id="gpt-5.4-mini",
            provider_model="gpt-5.4-mini",
            price_input_per_million=75,
            price_output_per_million=450,
            price_per_image_cents=0.0,
            billable_sku="legacy-gpt-5.4-mini-text",
        )
        resolved = SimpleNamespace(
            public_model=public_model,
            backend=SimpleNamespace(model_id="gpt-5.4-mini"),
            route_reason="catalog:gpt-5.4-mini:legacy_auto",
            lock_model_selection=False,
        )
        db = _FakeDB(execute_results=[_ScalarOneOrNoneResult(alias), _ScalarOneOrNoneResult(price)])

        with patch.object(station_runtime.model_registry, "resolve_public_model", return_value=resolved) as resolve_model:
            result = await station_runtime.resolve_station_model_for_user(
                db,
                user,
                "fast",
                "chat/completions",
            )

        resolve_model.assert_called_once()
        self.assertEqual(resolve_model.call_args.args[0], "gpt-5.4-mini")
        self.assertEqual(result.display_model, "fast")
        self.assertEqual(result.station_alias, "fast")
        self.assertEqual(result.resolved_public_model, "gpt-5.4-mini")
        self.assertEqual(result.retail_input_per_million, 120)
        self.assertEqual(result.retail_output_per_million, 720)
        self.assertEqual(result.wholesale_input_per_million, 75)
        self.assertEqual(result.wholesale_output_per_million, 450)
        self.assertEqual(result.price_version, 3)

    async def test_station_alias_resolution_rejects_inactive_station(self):
        user = SimpleNamespace(
            id="u_customer",
            _station_context={
                "station_id": "st_1",
                "slug": "stone",
                "status": "suspended",
                "mode": "commission_station",
            },
        )

        with self.assertRaises(HTTPException) as ctx:
            await station_runtime.resolve_station_model_for_user(
                _FakeDB(),
                user,
                "fast",
                "chat/completions",
            )

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "station suspended")

    async def test_usage_buffer_records_station_retail_and_wholesale_costs(self):
        buffer = UsageBuffer()

        await buffer.add(
            "u_customer",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            requests=1,
            endpoint="chat/completions",
            model="fast",
            customer_model_alias="fast",
            provider_model="gpt-5.4-mini",
            price_input_per_million=120,
            price_output_per_million=720,
            billable_sku="legacy-gpt-5.4-mini-text",
            station_id="st_1",
            station_alias="fast",
            resolved_public_model="gpt-5.4-mini",
            wholesale_price_input_per_million=75,
            wholesale_price_output_per_million=450,
            price_version=3,
        )

        _, _, request_logs = await buffer.snapshot_and_reset()

        self.assertEqual(len(request_logs), 1)
        log = request_logs[0]
        self.assertEqual(log["station_id"], "st_1")
        self.assertEqual(log["station_alias"], "fast")
        self.assertEqual(log["resolved_public_model"], "gpt-5.4-mini")
        self.assertEqual(log["cost_cents"], 840)
        self.assertEqual(log["retail_charge_cents"], 840)
        self.assertEqual(log["wholesale_cost_cents"], 525)
        self.assertEqual(log["price_version"], 3)

    def test_usage_pricing_kwargs_can_override_cached_input_to_full_station_price(self):
        public_model = SimpleNamespace(
            pricing_mode="multiplier",
            model_multiplier=1.0,
            output_multiplier=1.0,
            cache_read_multiplier=0.1,
            image_multiplier=1.0,
            video_multiplier=1.0,
            base_price_input_per_million=100,
            base_price_output_per_million=500,
            base_price_per_image_cents=0.0,
            base_price_per_video_cents=0.0,
            effective_cached_input_per_million=12.0,
            price_input_per_million=120,
            price_version=4,
        )
        station_model = station_runtime.StationResolvedModel(
            resolved_model=SimpleNamespace(
                public_model=public_model,
                backend=SimpleNamespace(model_id="gpt-5.4-mini"),
            ),
            display_model="fast",
            station_id="st_1",
            station_alias="fast",
            resolved_public_model="gpt-5.4-mini",
            retail_input_per_million=180,
            retail_output_per_million=720,
            retail_price_per_image_cents=0.0,
            wholesale_input_per_million=75,
            wholesale_output_per_million=450,
            wholesale_price_per_image_cents=0.0,
            price_version=3,
        )

        payload = station_runtime.usage_pricing_kwargs(
            public_model,
            station_model,
            user_cache_read_multiplier_override=1.0,
        )

        self.assertEqual(payload["cache_read_multiplier"], 1.0)
        self.assertEqual(payload["effective_cached_input_per_million"], 180)
        self.assertEqual(payload["price_version"], 3)


if __name__ == "__main__":
    unittest.main()
