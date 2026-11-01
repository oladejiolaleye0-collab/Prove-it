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
import prove_contract as pc  # noqa: E402
import prove_it  # noqa: E402
import stop_hook  # noqa: E402

HAVE_GIT = shutil.which("git") is not None
PY = sys.executable


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def contract(**overrides):
    data = {
        "task": "double the price",
        "scope": ["shop.py", "test_shop.py"],
        "criteria": [{"id": "c1", "text": "price doubles", "command": [PY, "-m", "unittest", "-q", "test_shop"]}],
    }
    data.update(overrides)
    return data


# --------------------------------------------------------------------------
# prove_contract: pure functions
# --------------------------------------------------------------------------


class ParseTests(unittest.TestCase):
    def test_valid_contract_is_normalized(self):
        parsed = pc.parse_contract(contract(amendments=[{"scope": ["docs/"], "reason": "docs"}]))
        self.assertEqual(parsed["criteria"][0]["fails_before"], False)
        self.assertEqual(parsed["amendments"][0]["scope"], ["docs/"])
        self.assertEqual(parsed["allow_test_changes"], [])

    def test_invalid_contracts_are_rejected(self):
        bad = [
            [],
            contract(task=""),
            contract(scope=[]),
            contract(criteria=[]),
            contract(criteria=[{"id": "a", "text": "x"}, {"id": "a", "text": "y"}]),
            contract(criteria=[{"id": "a", "text": "x", "fails_before": True}]),
            contract(criteria=[{"id": "a", "text": "x", "command": ""}]),
            contract(amendments=[{"scope": ["x"]}]),
            contract(allow_test_changes=[{"file": "t.py"}]),
        ]
        for data in bad:
            with self.assertRaises(pc.ContractError, msg=str(data)):
                pc.parse_contract(data)

    def test_hash_changes_with_content(self):
        a = pc.parse_contract(contract())
        b = pc.parse_contract(contract(task="something else"))
        self.assertNotEqual(pc.contract_hash(a), pc.contract_hash(b))
        self.assertEqual(pc.contract_hash(a), pc.contract_hash(pc.parse_contract(contract())))


class GlobTests(unittest.TestCase):
    def test_patterns(self):
        cases = [
            ("shop.py", "shop.py", True),
            ("src/shop.py", "shop.py", False),
            ("src/a.py", "src/*.py", True),
            ("src/sub/a.py", "src/*.py", False),
            ("src/sub/a.py", "src/**", True),
            ("src/sub/a.py", "src/", True),
            ("a/b/test_x.py", "**/test_*.py", True),
            ("test_x.py", "**/test_x.py", True),
            ("src\\win.py", "src/*.py", True),
        ]
        for path, pattern, expected in cases:
            self.assertEqual(pc.glob_match(path, pattern), expected, (path, pattern))

    def test_scope_includes_amendments_and_prove_it_folder(self):
        parsed = pc.parse_contract(contract(amendments=[{"scope": ["docs/**"], "reason": "r"}]))
        self.assertTrue(pc.in_scope("docs/a.md", parsed))
        self.assertTrue(pc.in_scope(".prove-it/claims.json", parsed))
        self.assertFalse(pc.in_scope("other.py", parsed))

    def test_test_file_detection(self):
        for path in ("test_a.py", "a_test.py", "tests/x.py", "pkg/tests/x.py", "src/a.test.ts",
                     "a.spec.js", "x_test.go", "web/__tests__/a.js"):
            self.assertTrue(pc.is_test_file(path), path)
        for path in ("shop.py", "src/testing_utils.py", "latest.py"):
            self.assertFalse(pc.is_test_file(path), path)


class IntegrityTests(unittest.TestCase):
    def check(self, files, added=None, removed=None):
        return pc.test_integrity(files, added or {}, removed or {})

    def test_removed_assertion_is_flagged(self):
        found = self.check({"test_a.py": "M"}, removed={"test_a.py": ["    assert x == 1"]})
        self.assertIn("1 fewer assertion", found[0][1])

    def test_changed_assertion_is_not_flagged(self):
        found = self.check({"test_a.py": "M"}, added={"test_a.py": ["    self.assertEqual(p(2), 4)"]},
                           removed={"test_a.py": ["    self.assertEqual(p(2), 2)"]})
        self.assertEqual(found, [])

    def test_added_skip_or_only_is_flagged(self):
        for line in ("@pytest.mark.skip", "@unittest.skip('x')", "it.only('x', () => {})", "xit('x')",
                     "    self.skipTest('flaky')", "describe.skip('x')", "t.Skip()"):
            found = self.check({"tests/a.py": "M"}, added={"tests/a.py": [line]})
            self.assertTrue(found and "skip or only" in found[0][1], line)

    def test_deleted_test_file_is_flagged(self):
        self.assertEqual(self.check({"tests/a.py": "D"}), [("tests/a.py", "test file deleted")])

    def test_non_test_files_are_ignored(self):
        self.assertEqual(self.check({"shop.py": "D"}, removed={"shop.py": ["assert x"]}), [])


class DroppedTests(unittest.TestCase):
    def test_removed_criterion_and_weakened_fails_before(self):
        locked = pc.parse_contract(contract(criteria=[
            {"id": "c1", "text": "a", "command": "x", "fails_before": True},
            {"id": "c2", "text": "b", "command": "y"},
        ], amendments=[{"scope": ["d"], "reason": "r"}]))
        now = pc.parse_contract(contract(criteria=[{"id": "c1", "text": "a", "command": "x"}]))
        problems = pc.dropped_since(locked, now)
        self.assertEqual(len(problems), 3)

    def test_additions_are_fine(self):
        locked = pc.parse_contract(contract())
        now = pc.parse_contract(contract(criteria=contract()["criteria"] + [{"id": "c2", "text": "more"}],
                                         scope=["shop.py"]))
        self.assertEqual(pc.dropped_since(locked, now), [])


# --------------------------------------------------------------------------
# prove_it.py with a contract
# --------------------------------------------------------------------------


@unittest.skipUnless(HAVE_GIT, "git is required")
class RepoBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name).resolve()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "Test")
        self.write(".prove-it.json", json.dumps({"test": False, "build": False, "lint": False}))
        self.write("shop.py", "def price(x):\n    return x\n")
        self.write("test_shop.py", "import unittest\nfrom shop import price\n\n"
                                   "class T(unittest.TestCase):\n    def test_price(self):\n"
                                   "        self.assertEqual(price(2), 2)\n")
        self.write("other.py", "z = 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def set_contract(self, data):
        self.write(".prove-it/contract.json", json.dumps(data))

    def double_price(self):
        self.write("shop.py", "def price(x):\n    return x * 2\n")
        self.write("test_shop.py", (self.repo / "test_shop.py").read_text().replace("price(2), 2", "price(2), 4"))

    def run_json(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = prove_it.main(["--repo", str(self.repo), "--json", *extra])
        return code, json.loads(out.getvalue())


class ContractRunTests(RepoBase):
    def test_in_scope_change_with_passing_criterion(self):
        self.double_price()
        self.set_contract(contract())
        code, data = self.run_json("--no-log")
        self.assertEqual(data["contract"]["out_of_scope"], [])
        self.assertEqual(data["contract"]["criteria"][0]["verdict"], "SUPPORTED")
        self.assertEqual(code, 0)

    def test_out_of_scope_change_fails(self):
        self.double_price()
        self.write("other.py", "z = 2\n")
        self.set_contract(contract())
        code, data = self.run_json("--no-log")
        self.assertEqual(data["contract"]["out_of_scope"], ["other.py"])
        self.assertEqual(data["state"], "FAILED")
        self.assertTrue(any("outside the contract's scope: other.py" in f for f in data["failures"]))

    def test_amendment_brings_file_into_scope(self):
        self.double_price()
        self.write("other.py", "z = 2\n")
        self.set_contract(contract(amendments=[{"scope": ["other.py"], "reason": "shared constant"}]))
        code, data = self.run_json("--no-log")
        self.assertEqual(data["contract"]["out_of_scope"], [])
        self.assertNotEqual(data["state"], "FAILED")

    def test_criterion_without_command_is_unverified(self):
        self.double_price()
        self.set_contract(contract(criteria=[{"id": "c1", "text": "feels right"}]))
        code, data = self.run_json("--no-log")
        self.assertEqual(data["contract"]["criteria"][0]["verdict"], "UNVERIFIED")
        self.assertTrue(any("criterion not verified: c1" in g for g in data["not_verified"]))

    def test_failing_criterion_command_fails_the_run(self):
        self.write("shop.py", "def price(x):\n    return x * 3\n")  # wrong fix
        self.write("test_shop.py", (self.repo / "test_shop.py").read_text().replace("price(2), 2", "price(2), 4"))
        self.set_contract(contract())
        code, data = self.run_json("--no-log")
        result = data["contract"]["criteria"][0]
        self.assertEqual(result["verdict"], "NOT_SUPPORTED")
        self.assertIn("AssertionError", result["output_tail"])
        self.assertEqual(code, 1)

    def test_bad_contract_is_a_usage_error(self):
        self.set_contract({"task": "x"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(prove_it.main(["--repo", str(self.repo)]), 2)

    def test_log_records_contract(self):
        self.double_price()
        self.set_contract(contract())
        with contextlib.redirect_stdout(io.StringIO()):
            prove_it.main(["--repo", str(self.repo)])
        entry = json.loads((self.repo / ".prove-it" / "log.jsonl").read_text().splitlines()[-1])
        parsed = pc.parse_contract(contract())
        self.assertEqual(entry["contract_hash"], pc.contract_hash(parsed))
        self.assertEqual(entry["criteria"], [{"id": "c1", "verdict": "SUPPORTED"}])

    def test_text_report_has_contract_sections(self):
        self.double_price()
        self.set_contract(contract())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            prove_it.main(["--repo", str(self.repo), "--no-log"])
        self.assertIn("CONTRACT", out.getvalue())
        self.assertIn("[SUPPORTED] c1: price doubles", out.getvalue())


class FailsBeforeTests(RepoBase):
    def fb_contract(self, **criterion):
        item = {"id": "c1", "text": "price doubles", "command": [PY, "-m", "unittest", "-q", "test_shop"],
                "fails_before": True}
        item.update(criterion)
        return contract(criteria=[item])

    def test_real_regression_test_fails_before_and_passes_now(self):
        self.double_price()
        self.set_contract(self.fb_contract())
        code, data = self.run_json("--no-log")
        details = data["contract"]["criteria"][0]["details"]
        self.assertEqual(data["contract"]["criteria"][0]["verdict"], "SUPPORTED", details)
        self.assertIn("fails on the code before the change", details[1][1])

    def test_test_that_passes_without_the_change_is_rejected(self):
        self.double_price()
        self.write("test_extra.py", "import unittest\n\nclass E(unittest.TestCase):\n"
                                    "    def test_ok(self):\n        self.assertTrue(True)\n")
        self.set_contract(self.fb_contract(command=[PY, "-m", "unittest", "-q", "test_extra"],
                                           tests=["test_extra.py"]))
        self.set_contract(dict(self.fb_contract(command=[PY, "-m", "unittest", "-q", "test_extra"],
                                                tests=["test_extra.py"]),
                               scope=["shop.py", "test_shop.py", "test_extra.py"]))
        code, data = self.run_json("--no-log")
        result = data["contract"]["criteria"][0]
        self.assertEqual(result["verdict"], "NOT_SUPPORTED")
        self.assertIn("does not show the change made the difference", result["details"][1][1])

    def test_environment_failure_before_is_unverified(self):
        self.double_price()
        self.write("marker.txt", "x")
        script = "import os, sys; sys.exit(0 if os.path.exists('marker.txt') else 127)"
        self.set_contract(dict(self.fb_contract(command=[PY, "-c", script]),
                               scope=["shop.py", "test_shop.py", "marker.txt"]))
        code, data = self.run_json("--no-log")
        self.assertEqual(data["contract"]["criteria"][0]["verdict"], "UNVERIFIED")

    def test_worktree_is_removed_and_linked_folders_survive(self):
        self.double_price()
        keep = self.write("node_modules/pkg/keep.txt", "precious")
        (self.repo / ".gitignore").write_text("node_modules/\n.prove-it/\n")
        self.set_contract(dict(self.fb_contract(), scope=["shop.py", "test_shop.py", ".gitignore"]))
        self.run_json("--no-log")
        self.assertEqual(keep.read_text(), "precious")
        worktrees = [line for line in git(self.repo, "worktree", "list").splitlines() if line.strip()]
        self.assertEqual(len(worktrees), 1, worktrees)

    def test_fails_before_from_a_subfolder(self):
        self.write("pkg/calc.py", "def f():\n    return 1\n")
        self.write("pkg/test_calc.py", "import unittest\nfrom calc import f\n\nclass C(unittest.TestCase):\n"
                                       "    def test_f(self):\n        self.assertEqual(f(), 1)\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "pkg")
        self.write("pkg/calc.py", "def f():\n    return 2\n")
        self.write("pkg/test_calc.py", (self.repo / "pkg/test_calc.py").read_text().replace("f(), 1", "f(), 2"))
        sub = self.repo / "pkg"
        self.write("pkg/.prove-it/contract.json", json.dumps({
            "task": "f returns 2", "scope": ["calc.py", "test_calc.py"],
            "criteria": [{"id": "c1", "text": "f() == 2", "command": [PY, "-m", "unittest", "-q", "test_calc"],
                          "fails_before": True}],
        }))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            prove_it.main(["--repo", str(sub), "--json", "--no-log"])
        data = json.loads(out.getvalue())
        self.assertEqual(data["contract"]["criteria"][0]["verdict"], "SUPPORTED",
                         data["contract"]["criteria"][0]["details"])


class IntegrityRunTests(RepoBase):
    def test_weakened_test_fails_without_contract(self):
        self.write("test_shop.py", "import unittest\n\nclass T(unittest.TestCase):\n"
                                   "    def test_price(self):\n        pass\n")
        code, data = self.run_json("--no-log")
        self.assertEqual(data["state"], "FAILED")
        self.assertTrue(any(f.startswith("test weakened: test_shop.py") for f in data["failures"]))

    def test_contract_can_allow_a_test_change_with_a_reason(self):
        self.write("test_shop.py", "import unittest\n\nclass T(unittest.TestCase):\n"
                                   "    def test_price(self):\n        pass\n")
        self.set_contract(contract(criteria=[{"id": "c1", "text": "x", "command": [PY, "-c", "pass"]}],
                                   allow_test_changes=[{"file": "test_shop.py", "reason": "price rule removed"}]))
        code, data = self.run_json("--no-log")
        self.assertNotEqual(data["state"], "FAILED")
        self.assertTrue(any("allowed by contract" in g for g in data["not_verified"]))


# --------------------------------------------------------------------------
# Hook: PreToolUse guard and contract-aware Stop
# --------------------------------------------------------------------------


class GuardTests(RepoBase):
    def edit(self, path, tool="Edit", session="s", **extra):
        event = {"hook_event_name": "PreToolUse", "cwd": str(self.repo), "session_id": session,
                 "tool_name": tool, "tool_input": {"file_path": str(self.repo / path), **extra}}
        return stop_hook.guard(event)

    def bash(self, command):
        return stop_hook.guard({"hook_event_name": "PreToolUse", "cwd": str(self.repo), "session_id": "s",
                                "tool_name": "Bash", "tool_input": {"command": command}})

    def assertDenied(self, payload, words):
        self.assertIsNotNone(payload)
        out = payload["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn(words, out["permissionDecisionReason"])

    def config(self, **hook):
        self.write(".prove-it.json", json.dumps({"test": False, "build": False, "lint": False, "hook": hook}))

    def test_edit_without_contract_is_denied(self):
        self.assertDenied(self.edit("shop.py"), "write .prove-it/contract.json before editing")

    def test_optional_and_off_modes_allow_edits_without_contract(self):
        for mode in ("optional", "off"):
            self.config(contract=mode)
            self.assertIsNone(self.edit("shop.py"), mode)

    def test_in_scope_allowed_out_of_scope_denied(self):
        self.set_contract(contract())
        self.assertIsNone(self.edit("shop.py"))
        payload = self.edit("other.py")
        self.assertDenied(payload, "other.py is outside the contract's scope")
        self.assertIn("amendments", payload["hookSpecificOutput"]["permissionDecisionReason"])

    def test_amendment_allows_the_file(self):
        self.set_contract(contract(amendments=[{"scope": ["other.py"], "reason": "needed"}]))
        self.assertIsNone(self.edit("other.py"))

    def test_relative_paths_and_new_files(self):
        self.set_contract(contract(scope=["src/**"]))
        event = {"hook_event_name": "PreToolUse", "cwd": str(self.repo), "session_id": "s",
                 "tool_name": "Write", "tool_input": {"file_path": "src/new/thing.py", "content": "x"}}
        self.assertIsNone(stop_hook.guard(event))

    def test_outside_the_project_is_not_guarded(self):
        outside = Path(tempfile.gettempdir()) / "prove-it-outside.txt"
        event = {"hook_event_name": "PreToolUse", "cwd": str(self.repo), "session_id": "s",
                 "tool_name": "Write", "tool_input": {"file_path": str(outside), "content": "x"}}
        self.assertIsNone(stop_hook.guard(event))

    def test_protected_files_are_denied(self):
        self.set_contract(contract(scope=["**"]))
        for path in (".prove-it.json", ".prove-it/log.jsonl", ".claude/settings.json"):
            self.assertDenied(self.edit(path), "only the user may change it")

    def test_shell_commands_touching_records_are_denied(self):
        self.assertDenied(self.bash("echo {} > .prove-it/log.jsonl"), "only the user may change")
        self.assertDenied(self.bash("sed -i s/x/y/ .prove-it.json"), "only the user may change")
        self.assertIsNone(self.bash(f'"{PY}" scripts/prove_it.py --history  # reads log.jsonl'))
        self.assertIsNone(self.bash("python -m unittest"))

    def test_invalid_contract_write_is_denied(self):
        payload = self.edit(".prove-it/contract.json", tool="Write", content=json.dumps({"task": "x"}))
        self.assertDenied(payload, "contract is not valid")
        self.assertIsNone(self.edit(".prove-it/contract.json", tool="Write", content=json.dumps(contract())))

    def test_criteria_cannot_be_dropped_after_editing_starts(self):
        self.set_contract(contract())
        self.assertIsNone(self.edit("shop.py"))  # locks the contract
        weaker = contract(criteria=[{"id": "c9", "text": "something easier"}])
        self.assertDenied(self.edit(".prove-it/contract.json", tool="Write", content=json.dumps(weaker)),
                          "criterion 'c1' was removed")
        stronger = contract(criteria=contract()["criteria"] + [{"id": "c2", "text": "also this"}])
        self.assertIsNone(self.edit(".prove-it/contract.json", tool="Write", content=json.dumps(stronger)))

    def test_new_turn_unlocks_the_contract(self):
        self.set_contract(contract())
        self.edit("shop.py")
        stop_hook.record_turn_start({"cwd": str(self.repo), "session_id": "s"})
        weaker = contract(criteria=[{"id": "c9", "text": "new task"}])
        self.assertIsNone(self.edit(".prove-it/contract.json", tool="Write", content=json.dumps(weaker)))

    def test_turn_start_returns_protocol_context(self):
        payload = stop_hook.record_turn_start({"cwd": str(self.repo), "session_id": "s"})
        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn(".prove-it/contract.json", context)
        self.assertIn(str(stop_hook.VERIFIER), context)

    def test_main_prints_deny_json_for_pre_tool_use(self):
        event = {"hook_event_name": "PreToolUse", "cwd": str(self.repo), "session_id": "m",
                 "tool_name": "Edit", "tool_input": {"file_path": str(self.repo / "shop.py")}}
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(event))), contextlib.redirect_stdout(out):
            self.assertEqual(stop_hook.main(), 0)
        self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny")


class ContractStopTests(RepoBase):
    HONEST = "Done. c1 verified. Not verified: tests, build and lint checks are disabled."

    def stop(self, message, session="s"):
        return stop_hook.decide({"cwd": str(self.repo), "session_id": session,
                                 "last_assistant_message": message})

    def lock(self, session="s"):
        stop_hook.guard({"hook_event_name": "PreToolUse", "cwd": str(self.repo), "session_id": session,
                         "tool_name": "Edit", "tool_input": {"file_path": str(self.repo / "shop.py")}})

    def prove(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return prove_it.main(["--repo", str(self.repo)])

    def test_contract_run_backs_the_stop(self):
        self.set_contract(contract())
        self.lock()
        self.double_price()
        self.prove()
        self.assertIsNone(self.stop(self.HONEST))

    def test_work_under_a_contract_is_checked_whatever_the_wording(self):
        self.set_contract(contract())
        self.lock()
        self.double_price()
        payload = self.stop("Let me know what you think.")
        self.assertEqual(payload["decision"], "block")

    def test_contract_changed_after_run_needs_a_new_run(self):
        self.set_contract(contract())
        self.lock()
        self.double_price()
        self.prove()
        self.set_contract(contract(task="double the price, carefully"))
        self.assertIn("did not check the current contract", self.stop(self.HONEST)["reason"])

    def test_contract_weakened_on_disk_is_caught_at_stop(self):
        self.set_contract(contract())
        self.lock()
        self.double_price()
        self.set_contract(contract(criteria=[{"id": "c9", "text": "easier"}]))
        self.prove()
        self.assertIn("weakened after you started editing", self.stop(self.HONEST)["reason"])

    def test_out_of_scope_change_through_shell_fails_the_run(self):
        self.set_contract(contract())
        self.lock()
        self.double_price()
        self.write("other.py", "z = 99\n")  # e.g. done with sed, which the guard cannot see
        self.assertEqual(self.prove(), 1)
        self.assertIn("FAILED", self.stop("Done, everything works.")["reason"])


if __name__ == "__main__":
    unittest.main()
