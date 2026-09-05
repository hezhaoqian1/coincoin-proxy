import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import httpx


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


class CoinCoinGrokCliLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not _env_flag("COINCOIN_RUN_LIVE_GROK_CLI_TESTS"):
            raise unittest.SkipTest(
                "set COINCOIN_RUN_LIVE_GROK_CLI_TESTS=1 to run the live Grok CLI test"
            )
        cls.grok_path = shutil.which("grok")
        if cls.grok_path is None:
            raise unittest.SkipTest("official grok CLI is not installed")

        cls.base_url = os.getenv("COINCOIN_GROK_BASE_URL", "https://coincoin.ai/v1").rstrip("/")
        cls.api_key = os.getenv("COINCOIN_GROK_API_KEY", "").strip()
        if not cls.api_key:
            raise unittest.SkipTest("set COINCOIN_GROK_API_KEY to a disposable CoinCoin key")

    def test_real_grok_cli_web_search_is_recorded_by_coincoin(self) -> None:
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        marker = "COINCOIN_GROK_WEB_SEARCH_OK"

        with tempfile.TemporaryDirectory(prefix="coincoin-grok-live-") as home:
            grok_dir = Path(home) / ".grok"
            grok_dir.mkdir(parents=True)
            (grok_dir / "config.toml").write_text(
                "\n".join(
                    [
                        "[models]",
                        'default = "grok-4.5"',
                        'web_search = "grok-4.5"',
                        "",
                        '[model."coincoin-grok"]',
                        'model = "grok-4.5"',
                        f'base_url = "{self.base_url}"',
                        f'api_key = "{self.api_key}"',
                        'name = "Grok"',
                        'description = "Grok 4.5"',
                        'api_backend = "responses"',
                        "context_window = 1000000",
                        "supports_backend_search = true",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            env = {**os.environ, "HOME": home}
            completed = subprocess.run(
                [
                    self.grok_path,
                    "-p",
                    (
                        "Use web search to find the current published npm version of "
                        f"@xai-official/grok, then include {marker} in the answer."
                    ),
                    "-m",
                    "grok-4.5",
                    "--output-format",
                    "json",
                    "--max-turns",
                    "3",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=240,
                check=False,
            )

        safe_stderr = completed.stderr.replace(self.api_key, "[REDACTED]")
        self.assertEqual(completed.returncode, 0, safe_stderr[-2000:])
        self.assertIn(marker, completed.stdout)

        headers = {"Authorization": f"Bearer {self.api_key}"}
        deadline = time.monotonic() + 45
        last_payload = None
        while time.monotonic() < deadline:
            response = httpx.get(
                f"{self.base_url}/usage",
                params={"limit": 50, "start_date": started_at},
                headers=headers,
                timeout=30,
            )
            self.assertEqual(response.status_code, 200, response.text[:1000])
            last_payload = response.json()
            matching = [
                item
                for item in last_payload.get("data", [])
                if item.get("model") == "grok-4.5"
                and item.get("endpoint") in {"responses", "responses:stream"}
                and item.get("server_side_tool_usage_details", {}).get("web_search_calls", 0) > 0
            ]
            if matching:
                self.assertGreater(matching[0]["num_server_side_tools_used"], 0)
                return
            time.sleep(2)

        self.fail(
            "Grok CLI completed but /v1/usage did not expose web_search_calls; "
            f"last usage payload keys={sorted((last_payload or {}).keys())}"
        )


if __name__ == "__main__":
    unittest.main()
