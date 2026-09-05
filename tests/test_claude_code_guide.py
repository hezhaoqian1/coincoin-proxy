import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUIDE_PAGE = ROOT / "coincoin-web" / "src" / "pages" / "GuideDetail.jsx"


def _render_claude_unix_command() -> str:
    source = GUIDE_PAGE.read_text(encoding="utf-8")
    match = re.search(
        r"const claudeUnixCommand = `(?P<command>.*?)`\n\n"
        r"        const claudeWindowsCommand",
        source,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError("Claude Unix command template was not found")
    return (
        match.group("command")
        .replace(r"\${CLAUDE_CONFIG_DIR:-$HOME/.claude}", "${CLAUDE_CONFIG_DIR:-$HOME/.claude}")
        .replace("${SITE_ROOT}", "https://coincoin.example")
        .replace("${snippetKey}", "test-claude-key")
        .replace(r"\\n", r"\n")
    )


class ClaudeCodeGuideTests(unittest.TestCase):
    def test_unix_command_bypasses_silent_broken_path_python_and_verifies_config(self) -> None:
        command = _render_claude_unix_command()
        with tempfile.TemporaryDirectory(prefix="coincoin-claude-guide-") as home:
            home_path = Path(home)
            bin_path = home_path / "bin"
            claude_dir = home_path / ".claude"
            settings_path = claude_dir / "settings.json"
            started_path = home_path / "claude-started"
            bin_path.mkdir()
            claude_dir.mkdir()
            settings_path.write_text(
                json.dumps({"theme": "dark", "env": {"KEEP_ME": "yes"}}),
                encoding="utf-8",
            )
            broken_python = bin_path / "python3"
            broken_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            broken_python.chmod(0o755)
            claude = bin_path / "claude"
            claude.write_text(
                f"#!/bin/sh\nprintf started > {started_path!s}\n",
                encoding="utf-8",
            )
            claude.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "HOME": home,
                    "PATH": f"{bin_path}:/usr/bin:/bin",
                }
            )
            result = subprocess.run(
                ["/bin/sh", "-c", command],
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(settings["theme"], "dark")
            self.assertEqual(settings["env"]["KEEP_ME"], "yes")
            self.assertEqual(
                settings["env"]["ANTHROPIC_BASE_URL"],
                "https://coincoin.example",
            )
            self.assertEqual(
                settings["env"]["ANTHROPIC_AUTH_TOKEN"],
                "test-claude-key",
            )
            self.assertEqual(started_path.read_text(encoding="utf-8"), "started")
            self.assertIn("Claude Code settings verified", result.stdout)


if __name__ == "__main__":
    unittest.main()
