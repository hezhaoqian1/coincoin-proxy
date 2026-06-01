import json
import unittest
from types import SimpleNamespace

from app.channel_router import ChannelRouter, ModelChannelRouteSnapshot, ProviderChannelSnapshot, channel_router
from app.config import settings
from app.router import registry


class ChannelRouterTests(unittest.TestCase):
    def _public(self, public_id: str = "demo-model"):
        return SimpleNamespace(public_id=public_id, provider_model="provider-default", upstream_model="")

    def _backend(self):
        return SimpleNamespace(model_id="catalog-model", auth_style="azure")

    def test_selects_lowest_priority_channel(self) -> None:
        router = ChannelRouter()
        router.set_snapshot(
            [
                ProviderChannelSnapshot(channel_id="ch_slow", base_url="https://slow.example", api_key="slow", priority=10),
                ProviderChannelSnapshot(channel_id="ch_fast", base_url="https://fast.example", api_key="fast", priority=0),
            ],
            [
                ModelChannelRouteSnapshot(route_id="r_slow", public_model_id="demo-model", channel_id="ch_slow"),
                ModelChannelRouteSnapshot(route_id="r_fast", public_model_id="demo-model", channel_id="ch_fast"),
            ],
        )

        choice = router.select_for_model(self._public(), self._backend(), "responses")

        self.assertIsNotNone(choice)
        self.assertEqual(choice.channel_id, "ch_fast")
        self.assertEqual(choice.upstream_url, "https://fast.example")

    def test_weighted_choice_stays_within_best_priority_tier(self) -> None:
        router = ChannelRouter()
        router.set_snapshot(
            [
                ProviderChannelSnapshot(channel_id="ch_a", base_url="https://a.example", api_key="a", priority=0, weight=1),
                ProviderChannelSnapshot(channel_id="ch_b", base_url="https://b.example", api_key="b", priority=0, weight=10),
                ProviderChannelSnapshot(channel_id="ch_c", base_url="https://c.example", api_key="c", priority=5, weight=100),
            ],
            [
                ModelChannelRouteSnapshot(route_id="r_a", public_model_id="demo-model", channel_id="ch_a"),
                ModelChannelRouteSnapshot(route_id="r_b", public_model_id="demo-model", channel_id="ch_b"),
                ModelChannelRouteSnapshot(route_id="r_c", public_model_id="demo-model", channel_id="ch_c"),
            ],
        )

        seen = {
            router.select_for_model(self._public(), self._backend(), "responses").channel_id
            for _ in range(20)
        }

        self.assertTrue(seen <= {"ch_a", "ch_b"})
        self.assertNotIn("ch_c", seen)

    def test_failure_cooldown_excludes_channel_and_falls_back(self) -> None:
        router = ChannelRouter()
        router.set_snapshot(
            [
                ProviderChannelSnapshot(
                    channel_id="ch_primary",
                    base_url="https://primary.example",
                    api_key="primary",
                    priority=0,
                    allowed_fails=1,
                    cooldown_seconds=60,
                ),
                ProviderChannelSnapshot(channel_id="ch_backup", base_url="https://backup.example", api_key="backup", priority=10),
            ],
            [
                ModelChannelRouteSnapshot(route_id="r_primary", public_model_id="demo-model", channel_id="ch_primary"),
                ModelChannelRouteSnapshot(route_id="r_backup", public_model_id="demo-model", channel_id="ch_backup"),
            ],
        )

        router.record_failure("ch_primary", error_code="429")
        choice = router.select_for_model(self._public(), self._backend(), "responses")

        self.assertEqual(choice.channel_id, "ch_backup")

    def test_excluded_channel_is_not_reselected_for_same_request_fallback(self) -> None:
        router = ChannelRouter()
        router.set_snapshot(
            [
                ProviderChannelSnapshot(channel_id="ch_primary", base_url="https://primary.example", api_key="primary", priority=0),
                ProviderChannelSnapshot(channel_id="ch_peer", base_url="https://peer.example", api_key="peer", priority=0),
                ProviderChannelSnapshot(channel_id="ch_backup", base_url="https://backup.example", api_key="backup", priority=5),
            ],
            [
                ModelChannelRouteSnapshot(route_id="r_primary", public_model_id="demo-model", channel_id="ch_primary"),
                ModelChannelRouteSnapshot(route_id="r_peer", public_model_id="demo-model", channel_id="ch_peer"),
                ModelChannelRouteSnapshot(route_id="r_backup", public_model_id="demo-model", channel_id="ch_backup"),
            ],
        )

        choice = router.select_for_model(
            self._public(),
            self._backend(),
            "responses",
            exclude_channel_ids=("ch_primary",),
        )

        self.assertIsNotNone(choice)
        self.assertEqual(choice.channel_id, "ch_peer")

    def test_excluded_best_tier_falls_to_next_priority(self) -> None:
        router = ChannelRouter()
        router.set_snapshot(
            [
                ProviderChannelSnapshot(channel_id="ch_primary", base_url="https://primary.example", api_key="primary", priority=0),
                ProviderChannelSnapshot(channel_id="ch_backup", base_url="https://backup.example", api_key="backup", priority=5),
            ],
            [
                ModelChannelRouteSnapshot(route_id="r_primary", public_model_id="demo-model", channel_id="ch_primary"),
                ModelChannelRouteSnapshot(route_id="r_backup", public_model_id="demo-model", channel_id="ch_backup"),
            ],
        )

        choice = router.select_for_model(
            self._public(),
            self._backend(),
            "responses",
            exclude_channel_ids=("ch_primary",),
        )

        self.assertIsNotNone(choice)
        self.assertEqual(choice.channel_id, "ch_backup")


class RegistryChannelRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._originals = {
            "fixed_model": settings.fixed_model,
            "embedding_model": settings.embedding_model,
            "router_enabled": settings.router_enabled,
            "upstream_base_url": settings.upstream_base_url,
            "upstream_api_key": settings.upstream_api_key,
            "price_input_per_million": settings.price_input_per_million,
            "price_output_per_million": settings.price_output_per_million,
            "primary_auth_style": settings.primary_auth_style,
            "primary_strip_unsupported": settings.primary_strip_unsupported,
            "cheap_model": settings.cheap_model,
            "fallback_model": settings.fallback_model,
            "model_catalog_json": settings.model_catalog_json,
            "model_alias_overrides_path": settings.model_alias_overrides_path,
        }
        settings.fixed_model = "legacy-default"
        settings.embedding_model = "text-embedding-3-small"
        settings.router_enabled = False
        settings.upstream_base_url = "https://legacy.example/v1"
        settings.upstream_api_key = "legacy-key"
        settings.price_input_per_million = 100
        settings.price_output_per_million = 200
        settings.primary_auth_style = "azure"
        settings.primary_strip_unsupported = False
        settings.cheap_model = ""
        settings.fallback_model = ""
        settings.model_alias_overrides_path = ""
        settings.model_catalog_json = json.dumps(
            {
                "default_text_model": "demo-model",
                "models": [
                    {
                        "id": "demo-model",
                        "owned_by": "coincoin",
                        "provider_name": "OpenAI",
                        "provider_model": "demo-provider",
                        "capabilities": ["chat/completions", "responses"],
                        "routing_mode": "direct",
                        "delivery_lane": "upstream_direct",
                        "upstream_model": "catalog-model",
                        "upstream_url": "https://catalog.example/v1",
                        "api_key": "catalog-key",
                        "auth_style": "bearer",
                        "price_input_per_million": 100,
                        "price_output_per_million": 200,
                    }
                ],
            }
        )
        channel_router.clear_snapshot()
        registry._initialized = False
        registry.init_from_settings()

    def tearDown(self) -> None:
        for key, value in self._originals.items():
            setattr(settings, key, value)
        channel_router.clear_snapshot()
        registry.clear_runtime_alias_overrides()
        registry.clear_runtime_pricing_overrides()
        registry._initialized = False

    def test_registry_uses_db_channel_route_when_configured(self) -> None:
        channel_router.set_snapshot(
            [
                ProviderChannelSnapshot(
                    channel_id="ch_db",
                    provider_platform="new_api",
                    channel_type="openai_compatible",
                    base_url="https://db-route.example/v1",
                    api_key="db-key",
                    auth_style="bearer",
                    priority=0,
                )
            ],
            [
                ModelChannelRouteSnapshot(
                    route_id="mcr_1",
                    public_model_id="demo-model",
                    endpoint="responses",
                    channel_id="ch_db",
                    upstream_model="db-upstream-model",
                )
            ],
        )

        resolved = registry.resolve_public_model("demo-model", "responses")

        self.assertEqual(resolved.backend.channel_id, "ch_db")
        self.assertEqual(resolved.backend.model_id, "db-upstream-model")
        self.assertEqual(resolved.backend.upstream_url, "https://db-route.example/v1")
        self.assertIn(":channel:ch_db", resolved.route_reason)

    def test_registry_resolves_channel_fallback_with_attempt_metadata(self) -> None:
        channel_router.set_snapshot(
            [
                ProviderChannelSnapshot(channel_id="ch_primary", base_url="https://primary.example/v1", api_key="primary", priority=0),
                ProviderChannelSnapshot(channel_id="ch_backup", base_url="https://backup.example/v1", api_key="backup", priority=5),
            ],
            [
                ModelChannelRouteSnapshot(route_id="mcr_primary", public_model_id="demo-model", endpoint="responses", channel_id="ch_primary"),
                ModelChannelRouteSnapshot(route_id="mcr_backup", public_model_id="demo-model", endpoint="responses", channel_id="ch_backup"),
            ],
        )

        resolved = registry.resolve_public_model("demo-model", "responses")
        fallback = registry.resolve_channel_fallback(
            resolved.public_model,
            resolved.backend,
            "responses",
            exclude_channel_ids=(resolved.backend.channel_id,),
        )

        self.assertIsNotNone(fallback)
        self.assertEqual(fallback.channel_id, "ch_backup")
        self.assertEqual(fallback.fallback_from_channel_id, "ch_primary")
        self.assertEqual(fallback.route_attempt, 1)

    def test_registry_keeps_catalog_route_when_no_db_route_exists(self) -> None:
        resolved = registry.resolve_public_model("demo-model", "responses")

        self.assertEqual(resolved.backend.channel_id, "")
        self.assertEqual(resolved.backend.model_id, "catalog-model")
        self.assertEqual(resolved.backend.upstream_url, "https://catalog.example/v1")
        self.assertEqual(resolved.route_reason, "catalog:demo-model:upstream_direct")


if __name__ == "__main__":
    unittest.main()
