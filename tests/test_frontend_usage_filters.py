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

    def test_usage_table_displays_and_exports_server_side_tools(self) -> None:
        usage_page = (
            Path(__file__).resolve().parents[1]
            / "coincoin-web"
            / "src"
            / "pages"
            / "Usage.jsx"
        ).read_text(encoding="utf-8")

        self.assertIn("function formatServerSideToolUsage", usage_page)
        self.assertIn("服务端工具", usage_page)
        self.assertIn("server_side_tool_usage_details", usage_page)
        self.assertIn("num_server_side_tools_used", usage_page)

        admin_page = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "static"
            / "admin.html"
        ).read_text(encoding="utf-8")
        self.assertIn("<th>服务端工具</th>", admin_page)
        self.assertIn("toolDetails.web_search_calls", admin_page)


if __name__ == "__main__":
    unittest.main()
