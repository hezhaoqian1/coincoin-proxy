import json
import unittest
from types import SimpleNamespace

from app.channel_router import ChannelRouter, ModelChannelRouteSnapshot, ProviderChannelSnapshot, channel_router
from app.config import settings
from app.router import ModelCapabilityError, registry


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

    def test_affinity_choice_is_stable_within_best_priority_tier(self) -> None:
        router = ChannelRouter()
        router.set_snapshot(
            [
                ProviderChannelSnapshot(channel_id="ch_a", base_url="https://a.example", api_key="a", priority=0, weight=1),
                ProviderChannelSnapshot(channel_id="ch_b", base_url="https://b.example", api_key="b", priority=0, weight=1),
                ProviderChannelSnapshot(channel_id="ch_c", base_url="https://c.example", api_key="c", priority=5, weight=100),
            ],
            [
                ModelChannelRouteSnapshot(route_id="r_a", public_model_id="demo-model", channel_id="ch_a"),
                ModelChannelRouteSnapshot(route_id="r_b", public_model_id="demo-model", channel_id="ch_b"),
                ModelChannelRouteSnapshot(route_id="r_c", public_model_id="demo-model", channel_id="ch_c"),
            ],
        )

        choices = [
            router.select_for_model(
                self._public(),
                self._backend(),
                "responses",
                affinity_key="user-key-model",
            ).channel_id
            for _ in range(10)
        ]

        self.assertEqual(len(set(choices)), 1)
        self.assertIn(choices[0], {"ch_a", "ch_b"})

    def test_affinity_fallback_excludes_failed_channel(self) -> None:
        router = ChannelRouter()
        router.set_snapshot(
            [
                ProviderChannelSnapshot(channel_id="ch_a", base_url="https://a.example", api_key="a", priority=0, weight=1),
                ProviderChannelSnapshot(channel_id="ch_b", base_url="https://b.example", api_key="b", priority=0, weight=1),
                ProviderChannelSnapshot(channel_id="ch_c", base_url="https://c.example", api_key="c", priority=5, weight=1),
            ],
            [
                ModelChannelRouteSnapshot(route_id="r_a", public_model_id="demo-model", channel_id="ch_a"),
                ModelChannelRouteSnapshot(route_id="r_b", public_model_id="demo-model", channel_id="ch_b"),
                ModelChannelRouteSnapshot(route_id="r_c", public_model_id="demo-model", channel_id="ch_c"),
            ],
        )

        affinity = "user-key-model"
        first = router.select_for_model(
            self._public(),
            self._backend(),
            "responses",
            affinity_key=affinity,
        )
        fallback = router.select_for_model(
            self._public(),
            self._backend(),
            "responses",
            exclude_channel_ids=(first.channel_id,),
            affinity_key=affinity,
        )

        self.assertNotEqual(fallback.channel_id, first.channel_id)
        self.assertIn(fallback.channel_id, {"ch_a", "ch_b"})

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
                "default_video_model": "seedance-v2-720p",
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
                    },
                    {
                        "id": "claude-fable-5",
                        "owned_by": "anthropic",
                        "provider_name": "Anthropic",
                        "provider_model": "claude-fable-5",
                        "capabilities": ["chat/completions"],
                        "routing_mode": "route_only",
                        "delivery_lane": "route_only",
                        "upstream_model": "claude-fable-5",
                        "auth_style": "x-api-key",
                        "price_input_per_million": 1000,
                        "price_output_per_million": 5000,
                        "metadata": {"provider_protocol": "anthropic_messages"},
                    },
                    {
                        "id": "claude-sonnet-4-6",
                        "owned_by": "coincoin",
                        "provider_name": "",
                        "provider_model": "gpt-5.4-mini",
                        "capabilities": ["chat/completions", "responses"],
                        "routing_mode": "direct",
                        "delivery_lane": "upstream_direct",
                        "upstream_model": "gpt-5.4-mini",
                        "upstream_url": "https://catalog.example/v1",
                        "api_key": "catalog-key",
                        "auth_style": "bearer",
                        "price_input_per_million": 300,
                        "price_output_per_million": 1500,
                        "metadata": {"compat_family": "claude-code"},
                    },
                    {
                        "id": "claude-sonnet-5",
                        "owned_by": "coincoin",
                        "provider_name": "",
                        "provider_model": "gpt-5.4-mini",
                        "capabilities": ["chat/completions", "responses"],
                        "routing_mode": "direct",
                        "delivery_lane": "upstream_direct",
                        "upstream_model": "gpt-5.4-mini",
                        "upstream_url": "https://catalog.example/v1",
                        "api_key": "catalog-key",
                        "auth_style": "bearer",
                        "price_input_per_million": 300,
                        "price_output_per_million": 1500,
                        "metadata": {"compat_family": "claude-code"},
                    },
                    {
                        "id": "seedance-v2-720p",
                        "owned_by": "bytedance",
                        "provider_name": "Seedance",
                        "provider_model": "seedance-v2-720p",
                        "capabilities": ["videos/generations"],
                        "routing_mode": "direct",
                        "delivery_lane": "upstream_direct",
                        "upstream_model": "seedance-v2-720p",
                        "upstream_url": "https://api.wgspai.cn",
                        "api_key": "seedance-key",
                        "auth_style": "bearer",
                        "price_per_video_cents": 98,
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

    def test_route_only_model_requires_active_provider_route(self) -> None:
        with self.assertRaises(ModelCapabilityError):
            registry.resolve_public_model("claude-fable-5", "chat/completions")

    def test_claude_code_alias_requires_active_provider_route(self) -> None:
        public_model = registry.public_models["claude-sonnet-4-6"]

        self.assertEqual(public_model.routing_mode, "route_only")
        self.assertEqual(public_model.delivery_lane, "route_only")
        with self.assertRaises(ModelCapabilityError):
            registry.resolve_public_model("claude-sonnet-4-6", "chat/completions")
        with self.assertRaises(ModelCapabilityError):
            registry.resolve_public_model("claude-sonnet-5", "chat/completions")

    def test_claude_code_alias_resolves_through_anthropic_channel_route(self) -> None:
        channel_router.set_snapshot(
            [
                ProviderChannelSnapshot(
                    channel_id="ch_sixoner",
                    provider_platform="sixoner",
                    channel_type="anthropic_compatible",
                    base_url="https://sub.sixoner.com",
                    api_key="sixoner-key",
                    auth_style="x-api-key",
                    priority=0,
                    capabilities=("chat/completions",),
                )
            ],
            [
                ModelChannelRouteSnapshot(
                    route_id="mcr_sixoner_sonnet",
                    public_model_id="claude-sonnet-4-6",
                    endpoint="chat/completions",
                    channel_id="ch_sixoner",
                    upstream_model="claude-sonnet-4-6",
                    transform_profile="anthropic_messages",
                ),
                ModelChannelRouteSnapshot(
                    route_id="mcr_sixoner_sonnet_5",
                    public_model_id="claude-sonnet-5",
                    endpoint="chat/completions",
                    channel_id="ch_sixoner",
                    upstream_model="claude-sonnet-5",
                    transform_profile="anthropic_messages",
                )
            ],
        )

        resolved = registry.resolve_public_model("claude-sonnet-4-6", "chat/completions")

        self.assertEqual(resolved.public_model.delivery_lane, "route_only")
        self.assertEqual(resolved.backend.channel_id, "ch_sixoner")
        self.assertEqual(resolved.backend.model_id, "claude-sonnet-4-6")
        self.assertEqual(resolved.backend.upstream_url, "https://sub.sixoner.com")
        self.assertEqual(resolved.backend.channel_type, "anthropic_compatible")
        self.assertEqual(resolved.backend.transform_profile, "anthropic_messages")
        self.assertEqual(resolved.route_reason, "catalog:claude-sonnet-4-6:route_only:channel:ch_sixoner")

        resolved_sonnet_5 = registry.resolve_public_model("claude-sonnet-5", "chat/completions")
        self.assertEqual(resolved_sonnet_5.public_model.delivery_lane, "route_only")
        self.assertEqual(resolved_sonnet_5.backend.channel_id, "ch_sixoner")
        self.assertEqual(resolved_sonnet_5.backend.model_id, "claude-sonnet-5")
        self.assertEqual(resolved_sonnet_5.route_reason, "catalog:claude-sonnet-5:route_only:channel:ch_sixoner")

    def test_route_only_model_resolves_through_anthropic_channel_route(self) -> None:
        channel_router.set_snapshot(
            [
                ProviderChannelSnapshot(
                    channel_id="ch_anthropic",
                    provider_platform="claude_relay",
                    channel_type="anthropic_compatible",
                    base_url="https://claude-relay.example",
                    api_key="relay-key",
                    auth_style="x-api-key",
                    priority=0,
                    capabilities=("chat/completions",),
                )
            ],
            [
                ModelChannelRouteSnapshot(
                    route_id="mcr_fable",
                    public_model_id="claude-fable-5",
                    endpoint="chat/completions",
                    channel_id="ch_anthropic",
                    upstream_model="claude-fable-5",
                    transform_profile="anthropic_messages",
                )
            ],
        )

        resolved = registry.resolve_public_model("claude-fable-5", "chat/completions")

        self.assertEqual(resolved.public_model.delivery_lane, "route_only")
        self.assertEqual(resolved.backend.channel_id, "ch_anthropic")
        self.assertEqual(resolved.backend.channel_type, "anthropic_compatible")
        self.assertEqual(resolved.backend.transform_profile, "anthropic_messages")
        self.assertEqual(resolved.backend.model_id, "claude-fable-5")
        self.assertEqual(resolved.backend.auth_style, "x-api-key")
        self.assertEqual(resolved.route_reason, "catalog:claude-fable-5:route_only:channel:ch_anthropic")

    def test_registry_uses_db_channel_route_for_video_endpoint(self) -> None:
        channel_router.set_snapshot(
            [
                ProviderChannelSnapshot(
                    channel_id="ch_seedance",
                    provider_platform="seedance",
                    channel_type="openai_compatible",
                    base_url="https://seedance-route.example",
                    api_key="seedance-db-key",
                    auth_style="bearer",
                    priority=0,
                    capabilities=("videos/generations",),
                )
            ],
            [
                ModelChannelRouteSnapshot(
                    route_id="mcr_seedance",
                    public_model_id="seedance-v2-720p",
                    endpoint="videos/generations",
                    channel_id="ch_seedance",
                    upstream_model="seedance-v2-720p",
                )
            ],
        )

        resolved = registry.resolve_public_model(None, "videos/generations")

        self.assertEqual(resolved.public_model.public_id, "seedance-v2-720p")
        self.assertEqual(resolved.backend.channel_id, "ch_seedance")
        self.assertEqual(resolved.backend.model_id, "seedance-v2-720p")
        self.assertEqual(resolved.backend.upstream_url, "https://seedance-route.example")
        self.assertIn(":channel:ch_seedance", resolved.route_reason)


if __name__ == "__main__":
    unittest.main()
