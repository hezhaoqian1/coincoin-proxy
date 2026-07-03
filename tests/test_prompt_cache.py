import unittest
from types import SimpleNamespace

from app.prompt_cache import (
    build_channel_affinity_key,
    build_claude_code_prompt_cache_key,
    build_openai_prompt_cache_retention,
)


class PromptCacheTests(unittest.TestCase):
    def test_gpt_5_5_responses_gets_prompt_cache_key_and_retention(self) -> None:
        user = SimpleNamespace(id="u_test")
        public_model = SimpleNamespace(public_id="gpt-5.5", provider_model="gpt-5.5", upstream_model="", billable_sku="", metadata={})

        key = build_claude_code_prompt_cache_key(
            user,
            "k_test",
            "gpt-5.5",
            public_model,
            effective_backend_model="gpt-5.5",
            include_openai_models=True,
        )

        self.assertTrue(key.startswith("cc-"))
        self.assertEqual(len(key), 35)
        self.assertEqual(
            build_openai_prompt_cache_retention(
                "gpt-5.5",
                public_model,
                effective_backend_model="gpt-5.5",
            ),
            "24h",
        )

    def test_non_cache_model_without_codex_does_not_get_prompt_cache_key(self) -> None:
        user = SimpleNamespace(id="u_test")
        public_model = SimpleNamespace(public_id="gpt-5.4-mini", provider_model="gpt-5.4-mini", upstream_model="", billable_sku="", metadata={})

        key = build_claude_code_prompt_cache_key(
            user,
            "k_test",
            "gpt-5.4-mini",
            public_model,
            effective_backend_model="gpt-5.4-mini",
            include_openai_models=True,
        )

        self.assertEqual(key, "")
        self.assertEqual(
            build_openai_prompt_cache_retention(
                "gpt-5.5",
                public_model,
                effective_backend_model="gpt-5.4-mini",
            ),
            "",
        )

    def test_codex_model_gets_prompt_cache_key(self) -> None:
        user = SimpleNamespace(id="u_test")
        public_model = SimpleNamespace(public_id="gpt-5-codex", provider_model="gpt-5-codex", upstream_model="", billable_sku="", metadata={})

        key = build_claude_code_prompt_cache_key(
            user,
            "k_test",
            "gpt-5-codex",
            public_model,
            effective_backend_model="gpt-5-codex",
            include_openai_models=True,
        )

        self.assertTrue(key.startswith("cc-"))

    def test_openai_model_cache_key_requires_openai_surface_opt_in(self) -> None:
        user = SimpleNamespace(id="u_test")
        public_model = SimpleNamespace(public_id="gpt-5.5", provider_model="gpt-5.5", upstream_model="", billable_sku="", metadata={})

        key = build_claude_code_prompt_cache_key(
            user,
            "k_test",
            "gpt-5.5",
            public_model,
            effective_backend_model="gpt-5.5",
        )

        self.assertEqual(key, "")

    def test_channel_affinity_key_is_stable_per_user_key_endpoint_model(self) -> None:
        user = SimpleNamespace(id="u_test")

        first = build_channel_affinity_key(user, "k_test", "responses", "gpt-5.5")
        second = build_channel_affinity_key(user, "k_test", "responses", "gpt_5.5")
        other_key = build_channel_affinity_key(user, "k_other", "responses", "gpt-5.5")

        self.assertEqual(first, second)
        self.assertNotEqual(first, other_key)
