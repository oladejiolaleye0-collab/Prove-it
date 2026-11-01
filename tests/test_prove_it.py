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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import prove_it  # noqa: E402

HAVE_GIT = shutil.which("git") is not None


def run_main(*args):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = prove_it.main(list(args))
    return code, buffer.getvalue()


def run_json(*args):
    code, text = run_main(*args, "--json")
    return code, json.loads(text)


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def config(self, **kwargs):
        defaults = {"test": False, "build": False, "lint": False}
        defaults.update(kwargs)
        self.write(".prove-it.json", json.dumps(defaults))

    def claims(self, items):
        return str(self.write("claims.json", json.dumps(items)))

    def init_git(self):
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "Test")
        self.write("app.py", "def old():\n    return 1\n")
        self.write("other.py", "x = 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")

    def commit_all(self, message="more"):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)


class CheckTests(Base):
    def test_passing_check(self):
        self.config(test=[sys.executable, "-c", "print('ok')"])
        code, data = run_json("--repo", str(self.repo), "--checks", "test", "--no-log")
        self.assertEqual(code, 0)
        self.assertEqual(data["checks"][0]["status"], "PASS")
        self.assertEqual(data["state"], "VERIFIED")

    def test_failing_check_sets_failed_and_exit_1(self):
        self.config(test=[sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"])
        code, data = run_json("--repo", str(self.repo), "--checks", "test", "--no-log")
        self.assertEqual(code, 1)
        self.assertEqual(data["checks"][0]["status"], "FAIL")
        self.assertEqual(data["checks"][0]["exit_code"], 3)
        self.assertIn("boom", data["checks"][0]["output_tail"])
        self.assertEqual(data["state"], "FAILED")

    def test_timeout(self):
        self.config(test=[sys.executable, "-c", "import time; time.sleep(5)"])
        code, data = run_json("--repo", str(self.repo), "--checks", "test", "--timeout", "1", "--no-log")
        self.assertEqual(code, 1)
        self.assertEqual(data["checks"][0]["status"], "TIMEOUT")

    def test_missing_command_is_not_run_not_pass(self):
        self.config(test=["definitely-not-a-real-command-xyz"])
        code, data = run_json("--repo", str(self.repo), "--checks", "test", "--no-log")
        self.assertEqual(data["checks"][0]["status"], "NOT_RUN")
        self.assertEqual(data["state"], "PARTIAL")
        self.assertEqual(code, 0)

    def test_unconfigured_check_is_a_gap(self):
        code, data = run_json("--repo", str(self.repo), "--checks", "lint", "--no-log")
        self.assertEqual(data["checks"][0]["status"], "NOT_CONFIGURED")
        self.assertEqual(data["state"], "PARTIAL")

    def test_npm_placeholder_test_script_is_ignored(self):
        self.write(
            "package.json",
            json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}),
        )
        commands, _ = prove_it.detect_commands(self.repo)
        self.assertNotIn("test", commands)

    def test_node_scripts_are_detected(self):
        self.write("package.json", json.dumps({"scripts": {"test": "jest", "lint": "eslint ."}}))
        commands, _ = prove_it.detect_commands(self.repo)
        self.assertEqual(commands["test"], ["npm", "run", "test"])
        self.assertEqual(commands["lint"], ["npm", "run", "lint"])

    def test_config_false_disables_detection(self):
        self.write("package.json", json.dumps({"scripts": {"lint": "eslint ."}}))
        self.config(lint=False)
        commands, disabled = prove_it.detect_commands(self.repo)
        self.assertNotIn("lint", commands)
        self.assertIn("lint", disabled)

    def test_string_command_quotes_are_stripped_on_windows(self):
        argv = prove_it._to_argv('pytest -k "a b" C:\\proj\\tests', windows=True)
        self.assertEqual(argv, ["pytest", "-k", "a b", "C:\\proj\\tests"])

    def test_string_command_uses_posix_rules_elsewhere(self):
        self.assertEqual(prove_it._to_argv('pytest -k "a b"', windows=False), ["pytest", "-k", "a b"])

    def test_windows_store_python_alias_is_replaced(self):
        alias = "C:\\Users\\me\\AppData\\Local\\Microsoft\\WindowsApps\\python3.exe"
        with mock.patch("prove_it.shutil.which", return_value=alias):
            self.assertEqual(prove_it._find_exe("python3", windows=True), sys.executable)
        with mock.patch("prove_it.shutil.which", return_value=None):
            self.assertEqual(prove_it._find_exe("python", windows=True), sys.executable)
            self.assertIsNone(prove_it._find_exe("python", windows=False))
        real = "C:\\Python312\\python.exe"
        with mock.patch("prove_it.shutil.which", return_value=real):
            self.assertEqual(prove_it._find_exe("python", windows=True), real)

    def test_python3_check_runs_even_without_python3_on_path(self):
        self.config(test="python3 -c \"print('hi there')\"")
        with mock.patch("prove_it._find_exe", return_value=sys.executable):
            code, data = run_json("--repo", str(self.repo), "--checks", "test", "--no-log")
        self.assertEqual(data["checks"][0]["status"], "PASS")
        self.assertIn("hi there", data["checks"][0]["output_tail"])

    def test_bad_config_is_a_usage_error(self):
        self.write(".prove-it.json", "{not json")
        self.assertEqual(run_main("--repo", str(self.repo), "--no-log")[0], 2)


@unittest.skipUnless(HAVE_GIT, "git is required")
class ClaimTests(Base):
    def setUp(self):
        super().setUp()
        self.init_git()
        self.config()
        self.commit_all("add config")

    def verdicts(self, items, *extra):
        code, data = run_json("--repo", str(self.repo), "--claims", self.claims(items), "--no-log", *extra)
        return code, data

    def test_modified_file_is_supported(self):
        self.write("app.py", "def old():\n    return 2\n")
        code, data = self.verdicts([{"claim": "changed app", "files": ["app.py"]}])
        self.assertEqual(data["claims"][0]["verdict"], "SUPPORTED")

    def test_unchanged_file_is_not_supported(self):
        self.write("app.py", "def old():\n    return 2\n")
        code, data = self.verdicts([{"claim": "changed other", "files": ["other.py"]}])
        self.assertEqual(data["claims"][0]["verdict"], "NOT_SUPPORTED")
        self.assertEqual(code, 1)
        self.assertEqual(data["state"], "FAILED")

    def test_untracked_new_file_counts_as_changed(self):
        self.write("new_module.py", "def fresh():\n    pass\n")
        code, data = self.verdicts(
            [{"claim": "added module", "files": ["new_module.py"], "symbols": ["fresh"]}]
        )
        self.assertEqual(data["claims"][0]["verdict"], "SUPPORTED")

    def test_symbol_in_added_lines_is_supported(self):
        self.write("app.py", "def old():\n    return 1\n\ndef login():\n    return True\n")
        code, data = self.verdicts([{"claim": "added login", "files": ["app.py"], "symbols": ["login"]}])
        self.assertEqual(data["claims"][0]["verdict"], "SUPPORTED")

    def test_symbol_not_added_is_not_supported(self):
        self.write("app.py", "def old():\n    return 2\n")
        code, data = self.verdicts([{"claim": "added login", "files": ["app.py"], "symbols": ["login"]}])
        self.assertEqual(data["claims"][0]["verdict"], "NOT_SUPPORTED")

    def test_symbol_match_is_whole_word(self):
        self.write("app.py", "def old():\n    return 1\n\ndef relogin():\n    pass\n")
        code, data = self.verdicts([{"claim": "added login", "files": ["app.py"], "symbols": ["login"]}])
        self.assertEqual(data["claims"][0]["verdict"], "NOT_SUPPORTED")

    def test_added_line_starting_with_plus_signs_is_not_mistaken_for_header(self):
        self.write("app.py", "def old():\n    return 1\n++ marker_symbol\n")
        code, data = self.verdicts([{"claim": "marker", "files": ["app.py"], "symbols": ["marker_symbol"]}])
        self.assertEqual(data["claims"][0]["verdict"], "SUPPORTED")

    def test_tests_pass_claim_follows_the_real_check(self):
        self.write("app.py", "def old():\n    return 2\n")
        self.config(test=[sys.executable, "-c", "import sys; sys.exit(1)"])
        code, data = self.verdicts([{"claim": "tests pass", "kind": "tests_pass"}])
        self.assertEqual(data["claims"][0]["verdict"], "NOT_SUPPORTED")
        self.config(test=[sys.executable, "-c", "pass"])
        code, data = self.verdicts([{"claim": "tests pass", "kind": "tests_pass"}])
        self.assertEqual(data["claims"][0]["verdict"], "SUPPORTED")

    def test_tests_pass_claim_without_a_test_command_is_unverified(self):
        code, data = self.verdicts([{"claim": "tests pass", "kind": "tests_pass"}])
        self.assertEqual(data["claims"][0]["verdict"], "UNVERIFIED")

    def test_claim_command_that_passes_supports_the_claim(self):
        cmd = [sys.executable, "-c", "print('behaves')"]
        code, data = self.verdicts([{"claim": "negative prices raise", "command": cmd}])
        self.assertEqual(data["claims"][0]["verdict"], "SUPPORTED")
        self.assertIn("exited 0", data["claims"][0]["details"][0][1])

    def test_claim_command_that_fails_contradicts_the_claim(self):
        cmd = [sys.executable, "-c", "import sys; print('AssertionError: -1 != 0'); sys.exit(1)"]
        code, data = self.verdicts([{"claim": "negative prices raise", "command": cmd}])
        self.assertEqual(data["claims"][0]["verdict"], "NOT_SUPPORTED")
        self.assertIn("AssertionError", data["claims"][0]["output_tail"])
        self.assertEqual(code, 1)

    def test_claim_command_not_found_is_unverified(self):
        code, data = self.verdicts([{"claim": "x", "command": "no-such-tool-xyz --check"}])
        self.assertEqual(data["claims"][0]["verdict"], "UNVERIFIED")

    def test_claim_command_combines_with_files(self):
        self.write("app.py", "def old():\n    return 2\n")
        cmd = [sys.executable, "-c", "import sys; sys.exit(2)"]
        code, data = self.verdicts([{"claim": "x", "files": ["app.py"], "command": cmd}])
        self.assertEqual(data["claims"][0]["verdict"], "NOT_SUPPORTED")

    def test_claim_command_failure_output_is_in_text_report(self):
        cmd = [sys.executable, "-c", "import sys; print('boom-line'); sys.exit(1)"]
        code, text = run_main("--repo", str(self.repo), "--claims", self.claims([{"claim": "x", "command": cmd}]), "--no-log")
        self.assertIn("    | boom-line", text)

    def test_unknown_kind_is_unverified_not_ignored(self):
        self.write("app.py", "def old():\n    return 2\n")
        code, data = self.verdicts([{"claim": "changed app, tests pass", "files": ["app.py"], "kind": "test_pass"}])
        self.assertEqual(data["claims"][0]["verdict"], "UNVERIFIED")
        self.assertTrue(any("unknown kind 'test_pass'" in gap for gap in data["not_verified"]))

    def test_claim_with_nothing_checkable_is_unverified(self):
        code, data = self.verdicts([{"claim": "it works great"}])
        self.assertEqual(data["claims"][0]["verdict"], "UNVERIFIED")
        self.assertEqual(code, 0)
        self.assertEqual(data["state"], "PARTIAL")

    def test_strict_turns_unverified_into_failure(self):
        code, data = self.verdicts([{"claim": "it works great"}], "--strict")
        self.assertEqual(code, 1)
        self.assertEqual(data["state"], "FAILED")

    def test_unclaimed_changes_are_listed(self):
        self.write("app.py", "def old():\n    return 2\n")
        self.write("other.py", "x = 2\n")
        code, data = self.verdicts([{"claim": "changed app", "files": ["app.py"]}])
        self.assertEqual(data["changed_but_not_claimed"], ["other.py"])

    def test_base_option_compares_against_a_commit(self):
        self.write("app.py", "def old():\n    return 2\n")
        git(self.repo, "commit", "-aq", "-m", "change")
        code, data = self.verdicts([{"claim": "changed app", "files": ["app.py"]}])
        self.assertEqual(data["claims"][0]["verdict"], "NOT_SUPPORTED")  # nothing vs HEAD
        code, data = self.verdicts([{"claim": "changed app", "files": ["app.py"]}], "--base", "HEAD~1")
        self.assertEqual(data["claims"][0]["verdict"], "SUPPORTED")

    def test_invalid_base_makes_diff_unavailable(self):
        code, data = self.verdicts([{"claim": "changed app", "files": ["app.py"]}], "--base", "nope")
        self.assertFalse(data["diff"]["available"])
        self.assertEqual(data["claims"][0]["verdict"], "UNVERIFIED")

    def test_option_like_base_is_rejected(self):
        code, _ = run_main("--repo", str(self.repo), "--base=-x", "--no-log")
        self.assertEqual(code, 2)

    def test_repo_option_pointing_at_a_subfolder(self):
        self.write("pkg/mod.py", "a = 1\n")
        self.write("pkg/.prove-it.json", json.dumps({"test": False, "build": False, "lint": False}))
        self.commit_all("add pkg")
        self.write("pkg/mod.py", "a = 1\ndef helper():\n    pass\n")
        self.write("pkg/new.py", "b = 2\n")
        self.write("app.py", "def old():\n    return 2\n")  # outside the subfolder
        sub = self.repo / "pkg"
        claims = self.write("pkg/claims.json", json.dumps([
            {"claim": "added helper", "files": ["mod.py"], "symbols": ["helper"]},
            {"claim": "added new", "files": ["new.py"]},
        ]))
        code, data = run_json("--repo", str(sub), "--claims", str(claims), "--no-log")
        self.assertEqual([c["verdict"] for c in data["claims"]], ["SUPPORTED", "SUPPORTED"])
        self.assertEqual(sorted(data["diff"]["files"]), ["mod.py", "new.py"])
        self.assertEqual(data["changed_but_not_claimed"], [])
        changed, _ = prove_it.working_changes(sub, "HEAD", frozenset({"claims.json"}))
        self.assertEqual(sorted(changed), ["mod.py", "new.py"])

    def test_large_untracked_file_is_fingerprinted_by_size(self):
        with mock.patch("prove_it.MAX_UNTRACKED_BYTES", 10):
            self.write("big.dat", "a" * 50)
            first = prove_it.working_changes(self.repo, "HEAD")[1]
            with mock.patch("pathlib.Path.read_bytes", side_effect=AssertionError("read big file")):
                self.assertEqual(prove_it.working_changes(self.repo, "HEAD")[1], first)
            self.write("big.dat", "a" * 60)
            self.assertNotEqual(prove_it.working_changes(self.repo, "HEAD")[1], first)

    def test_repo_with_no_commits(self):
        fresh = tempfile.TemporaryDirectory()
        self.addCleanup(fresh.cleanup)
        git(fresh.name, "init", "-q")
        Path(fresh.name, ".prove-it.json").write_text(json.dumps({"test": False, "build": False, "lint": False}))
        Path(fresh.name, "a.py").write_text("y = 1\n")
        claims = Path(fresh.name, "claims.json")
        claims.write_text(json.dumps([{"claim": "added a", "files": ["a.py"]}]))
        code, data = run_json("--repo", fresh.name, "--claims", str(claims), "--no-log")
        self.assertEqual(data["claims"][0]["verdict"], "SUPPORTED")


class NoGitTests(Base):
    def test_file_claims_are_unverified_without_git(self):
        self.config()
        code, data = run_json(
            "--repo", str(self.repo), "--claims",
            self.claims([{"claim": "changed app", "files": ["app.py"]}]), "--no-log",
        )
        self.assertFalse(data["diff"]["available"])
        self.assertEqual(data["claims"][0]["verdict"], "UNVERIFIED")


class InputTests(Base):
    def test_bad_claims_are_usage_errors(self):
        for bad in ('"just a string"', '[{"files": ["a.py"]}]', '[{"claim": "x", "files": "a.py"}]', "not json",
                    '[{"claim": "x", "command": ""}]', '[{"claim": "x", "command": [1, 2]}]'):
            path = self.write("claims.json", bad)
            code, _ = run_main("--repo", str(self.repo), "--claims", str(path), "--no-log")
            self.assertEqual(code, 2, bad)

    def test_unknown_check_name_is_rejected(self):
        code, _ = run_main("--repo", str(self.repo), "--checks", "deploy", "--no-log")
        self.assertEqual(code, 2)

    def test_claims_object_form_is_accepted(self):
        self.config()
        path = self.write("claims.json", json.dumps({"claims": [{"claim": "vague"}]}))
        code, data = run_json("--repo", str(self.repo), "--claims", str(path), "--no-log")
        self.assertEqual(len(data["claims"]), 1)


@unittest.skipUnless(HAVE_GIT, "git is required")
class LogTests(Base):
    def test_log_written_and_history_summarizes(self):
        self.init_git()
        self.config()
        self.write("app.py", "def old():\n    return 2\n")
        claims = self.claims([{"claim": "changed app", "files": ["app.py"]}])
        run_main("--repo", str(self.repo), "--claims", claims)
        run_main("--repo", str(self.repo), "--claims", claims)
        log = (self.repo / ".prove-it" / "log.jsonl").read_text().splitlines()
        self.assertEqual(len(log), 2)
        self.assertEqual(json.loads(log[0])["claims"][0]["verdict"], "SUPPORTED")
        self.assertTrue((self.repo / ".prove-it" / ".gitignore").exists())
        code, text = run_main("--repo", str(self.repo), "--history")
        self.assertEqual(code, 0)
        self.assertIn("2 run(s)", text)
        self.assertIn("SUPPORTED=2", text)

    def test_log_folder_is_not_reported_as_changed(self):
        self.init_git()
        self.config()
        self.commit_all("add config")
        run_main("--repo", str(self.repo))
        code, data = run_json("--repo", str(self.repo), "--no-log")
        self.assertFalse([f for f in data["diff"]["files"] if f.startswith(".prove-it/")])
        self.assertEqual(data["diff"]["files"], {})

    def test_no_log_flag(self):
        self.config()
        run_main("--repo", str(self.repo), "--no-log")
        self.assertFalse((self.repo / ".prove-it").exists())

    def test_history_with_no_log(self):
        code, text = run_main("--repo", str(self.repo), "--history")
        self.assertEqual(code, 0)
        self.assertIn("No prove-it history", text)


if __name__ == "__main__":
    unittest.main()
