import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ClaudeOpus5SupportTests(unittest.TestCase):
    def test_frontend_fallback_and_sorting_include_claude_opus_5_5(self) -> None:
        client = (ROOT / "coincoin-web" / "src" / "api" / "client.js").read_text(encoding="utf-8")
        public_models = (ROOT / "coincoin-web" / "src" / "hooks" / "usePublicModels.js").read_text(encoding="utf-8")

        self.assertIn("{ id: 'claude-opus-5-5'", client)
        self.assertIn("{ id: 'claude-opus-5', object", client)
        self.assertIn("coincoin_price_cache_creation_input_per_million: 500", client)
        self.assertIn("'claude-opus-5-5': 0", public_models)
        self.assertIn("'claude-opus-5': 1", public_models)

    def test_customer_guides_recommend_the_explicit_opus_5_5_model(self) -> None:
        docs = (ROOT / "coincoin-web" / "src" / "pages" / "Docs.jsx").read_text(encoding="utf-8")
        guide = (ROOT / "coincoin-web" / "src" / "pages" / "GuideDetail.jsx").read_text(encoding="utf-8")
        landing = (ROOT / "coincoin-web" / "src" / "pages" / "Landing.jsx").read_text(encoding="utf-8")

        self.assertIn("const CLAUDE_OPUS_OPTIONAL_MODEL_ID = 'claude-opus-5-5'", docs)
        self.assertIn("const CLAUDE_OPUS_OPTIONAL_MODEL_ID = 'claude-opus-5-5'", guide)
        self.assertIn("sonnet-4-6 · claude-opus-5-5", landing)


if __name__ == "__main__":
    unittest.main()
