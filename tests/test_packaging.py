import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import install  # noqa: E402
import prove_it  # noqa: E402

HAVE_GIT = shutil.which("git") is not None


def load(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


class PluginFilesTests(unittest.TestCase):
    def test_versions_agree(self):
        self.assertEqual(load(".claude-plugin/plugin.json")["version"], prove_it.__version__)
        self.assertIn(f"## {prove_it.__version__}", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))

    def test_marketplace_lists_the_plugin_at_the_repo_root(self):
        market = load(".claude-plugin/marketplace.json")
        plugin = load(".claude-plugin/plugin.json")
        self.assertEqual(market["plugins"][0]["name"], plugin["name"])
        self.assertEqual(market["plugins"][0]["source"], "./")
        self.assertTrue((ROOT / "SKILL.md").is_file())

    def test_plugin_hooks_cover_all_events_and_point_at_the_script(self):
        hooks = load("hooks/hooks.json")["hooks"]
        self.assertEqual(set(hooks), set(install.EVENTS))
        for event, groups in hooks.items():
            self.assertEqual(groups[0].get("matcher"), install.EVENTS[event])
            command = groups[0]["hooks"][0]["command"]
            self.assertIn('"${CLAUDE_PLUGIN_ROOT}/scripts/stop_hook.py"', command)
        self.assertTrue((ROOT / "scripts" / "stop_hook.py").is_file())

    def test_skill_front_matter(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: prove-it\ndescription: "))

    def test_version_flag(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            prove_it.main(["--version"])
        self.assertEqual(out.getvalue().strip(), f"prove-it {prove_it.__version__}")


class InstallerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name)
        self.settings = self.project / ".claude" / "settings.json"

    def run_install(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = install.main(["--project", str(self.project), *args])
        return code, out.getvalue()

    def current(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def test_install_creates_all_hooks_with_absolute_paths(self):
        self.run_install()
        hooks = self.current()["hooks"]
        self.assertEqual(set(hooks), set(install.EVENTS))
        command = hooks["Stop"][0]["hooks"][0]["command"]
        self.assertIn(Path(sys.executable).as_posix(), command)
        self.assertIn("scripts/stop_hook.py", command)
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], install.TOOL_MATCHER)

    def test_install_keeps_other_settings_and_is_idempotent(self):
        self.settings.parent.mkdir()
        other = {"type": "command", "command": "echo mine"}
        self.settings.write_text(json.dumps({
            "model": "opus",
            "hooks": {"Stop": [{"hooks": [other]}], "Notification": [{"hooks": [other]}]},
        }))
        self.run_install()
        self.run_install()
        data = self.current()
        self.assertEqual(data["model"], "opus")
        stop_commands = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
        self.assertEqual(stop_commands.count("echo mine"), 1)
        self.assertEqual(sum("stop_hook.py" in c for c in stop_commands), 1)
        self.assertEqual(data["hooks"]["Notification"], [{"hooks": [other]}])
        self.assertTrue(Path(str(self.settings) + ".bak").is_file())

    def test_uninstall_removes_only_prove_it(self):
        self.settings.parent.mkdir()
        self.settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}))
        self.run_install()
        code, out = self.run_install("--uninstall")
        self.assertIn("removed 4 Prove it hook(s)", out)
        self.assertEqual(self.current(), {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}})

    def test_dry_run_writes_nothing(self):
        code, out = self.run_install("--dry-run")
        self.assertIn("stop_hook.py", out)
        self.assertFalse(self.settings.exists())

    def test_invalid_settings_are_left_alone(self):
        self.settings.parent.mkdir()
        self.settings.write_text("{not json")
        with self.assertRaises(SystemExit):
            self.run_install()
        self.assertEqual(self.settings.read_text(), "{not json")


@unittest.skipUnless(HAVE_GIT, "git is required")
class DemoTests(unittest.TestCase):
    def test_demo_project_has_the_bug_and_working_hooks(self):
        sys.path.insert(0, str(ROOT / "examples"))
        import try_it

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name) / "demo"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(try_it.main([str(folder)]), 0)
        clean = subprocess.run(["git", "status", "--porcelain"], cwd=folder, capture_output=True, text=True)
        self.assertEqual(clean.stdout, "")
        settings = json.loads((folder / ".claude" / "settings.json").read_text())
        self.assertEqual(set(settings["hooks"]), set(install.EVENTS))
        # The shipped tests pass, and the bug is real.
        tests = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=folder, capture_output=True)
        self.assertEqual(tests.returncode, 0)
        bug = subprocess.run([sys.executable, "-c", "from cart import apply_discount as d; assert d(20.0, 10) == 18.0"],
                             cwd=folder, capture_output=True)
        self.assertNotEqual(bug.returncode, 0)
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            try_it.main([str(folder)])  # refuses to overwrite


if __name__ == "__main__":
    unittest.main()
