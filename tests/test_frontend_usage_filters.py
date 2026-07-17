import unittest
from pathlib import Path


class FrontendUsageFilterTests(unittest.TestCase):
    def test_usage_endpoint_filter_includes_claude_messages(self) -> None:
        usage_page = (
            Path(__file__).resolve().parents[1]
            / "coincoin-web"
            / "src"
            / "pages"
            / "Usage.jsx"
        ).read_text(encoding="utf-8")

        self.assertIn('<option value="messages">messages</option>', usage_page)
        self.assertIn(
            '<option value="messages:stream">messages:stream</option>',
            usage_page,
        )


if __name__ == "__main__":
    unittest.main()
