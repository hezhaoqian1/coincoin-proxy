import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class GrokBuildGuideTests(unittest.TestCase):
    def test_one_click_commands_replace_the_full_grok_config(self) -> None:
        guide_page = (
            ROOT / "coincoin-web" / "src" / "pages" / "GuideDetail.jsx"
        ).read_text(encoding="utf-8")

        self.assertIn("cat > \"$CONFIG\" <<'EOF'", guide_page)
        self.assertIn('$Block | Set-Content $Config -Encoding UTF8', guide_page)
        self.assertIn('[models]', guide_page)
        self.assertIn('default = "grok-4.5"', guide_page)
        self.assertIn('web_search = "grok-4.5"', guide_page)
        self.assertIn('[model.\"coincoin-grok\"]', guide_page)
        self.assertIn('model = "grok-4.5"', guide_page)
        self.assertIn('context_window = 1000000', guide_page)
        self.assertIn('supports_backend_search = true', guide_page)
        self.assertNotIn('GROK_CONFIG="$CONFIG" python3', guide_page)
        self.assertNotIn('$ModelsPattern =', guide_page)
        self.assertNotIn('grok inspect', guide_page)
        self.assertNotIn('-m grok-4.5', guide_page)

    def test_docs_example_matches_the_generated_grok_config(self) -> None:
        docs_page = (
            ROOT / "coincoin-web" / "src" / "pages" / "Docs.jsx"
        ).read_text(encoding="utf-8")

        self.assertIn('[model.\"coincoin-grok\"]', docs_page)
        self.assertIn('context_window = 1000000', docs_page)
        self.assertIn('supports_backend_search = true', docs_page)


if __name__ == "__main__":
    unittest.main()
