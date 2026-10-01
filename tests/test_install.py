"""Guarded marketplace setup tests; all Herdr calls use a fake binary."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("jq"), "installer requires jq")
class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        tools = self.base / "bin"
        tools.mkdir()
        herdr = tools / "fake-herdr"
        herdr.write_text("""#!/bin/sh
case "$1 $2" in
  'plugin list') printf '{"result":{"plugins":[{"plugin_id":"hessam.herdr-flow","plugin_root":"%s"}]}}\\n' "$FLOW_TEST_ROOT" ;;
  'plugin config-dir') printf '%s\\n' "$FLOW_TEST_CONFIG" ;;
  *) echo "Unexpected Herdr call: $*" >&2; exit 21 ;;
esac
""")
        herdr.chmod(0o755)
        for name in ("opencode", "codex", "claude"):
            tool = tools / name
            tool.write_text("#!/bin/sh\nexit 0\n")
            tool.chmod(0o755)
        self.config = self.base / "plugin-config"
        self.env = os.environ.copy()
        self.env.update({
            "HOME": str(self.home),
            "HERDR_ENV": "1",  # Fake context and fake Herdr binary; never contacts a live session.
            "HERDR_BIN_PATH": str(herdr),
            "FLOW_TEST_ROOT": str(ROOT),
            "FLOW_TEST_CONFIG": str(self.config),
            "PATH": f"{tools}:{os.environ['PATH']}",
        })
        self.env.pop("HERDR_PLUGIN_CONFIG_DIR", None)

    def setup_plugin(self):
        return subprocess.run(["bash", str(ROOT / "install.sh")], env=self.env, capture_output=True, text=True)

    def test_registered_marketplace_plugin_setup_is_repeatable(self):
        for _ in range(2):
            result = self.setup_plugin()
            self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(ROOT / "bin/herdr-flow", (self.home / ".local/bin/herdr-flow").resolve())
        self.assertEqual(ROOT / "skills/herdr-flow-plan", (self.home / ".agents/skills/herdr-flow-plan").resolve())
        self.assertEqual(ROOT / "commands/opencode/ai-plan.md", (self.home / ".config/opencode/commands/ai-plan.md").resolve())
        private = self.config / "config.json"
        self.assertEqual((ROOT / "config.json").read_text(), private.read_text())
        self.assertEqual(0o600, private.stat().st_mode & 0o777)

    def test_foreign_link_is_refused_without_partial_setup(self):
        foreign = self.home / ".claude/commands/ai-plan.md"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("owner's command")
        result = self.setup_plugin()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Refusing to replace existing path", result.stderr)
        self.assertEqual("owner's command", foreign.read_text())
        self.assertFalse((self.home / ".local/bin/herdr-flow").exists())
        self.assertFalse((self.config / "config.json").exists())

    def test_external_session_is_refused(self):
        self.env.pop("HERDR_ENV")
        result = self.setup_plugin()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Herdr-managed pane", result.stderr)
        self.assertFalse((self.home / ".local/bin/herdr-flow").exists())


if __name__ == "__main__":
    unittest.main()
