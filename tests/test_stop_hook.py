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
import stop_hook  # noqa: E402

HAVE_GIT = shutil.which("git") is not None
HONEST = "Done. Verified: app.py changed. Not verified: tests, build and lint are disabled."


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@unittest.skipUnless(HAVE_GIT, "git is required")
class StopHookTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "Test")
        self.config()
        self.write("app.py", "def old():\n    return 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def config(self, **hook):
        data = {"test": False, "build": False, "lint": False}
        if hook:
            data["hook"] = hook
        self.write(".prove-it.json", json.dumps(data))

    def prove(self, claims=None, claims_path=".prove-it/claims.json", *extra):
        if claims is None:
            claims = [{"claim": "changed app", "files": ["app.py"]}]
        path = self.write(claims_path, json.dumps(claims))
        with contextlib.redirect_stdout(io.StringIO()):
            return prove_it.main(["--repo", str(self.repo), "--claims", str(path), *extra])

    def turn_start(self, session="s1"):
        stop_hook.record_turn_start({"cwd": str(self.repo), "session_id": session})

    def stop(self, message="All done, the fix works.", session="s1"):
        return stop_hook.decide(
            {"cwd": str(self.repo), "session_id": session, "last_assistant_message": message}
        )

    def assertBlocked(self, payload, words):
        self.assertIsNotNone(payload)
        self.assertEqual(payload.get("decision"), "block")
        self.assertIn(words, payload["reason"])

    # -- basics ------------------------------------------------------------

    def test_no_changes_allows_stop(self):
        self.assertIsNone(self.stop())

    def test_done_claim_without_a_run_is_blocked(self):
        self.write("app.py", "def old():\n    return 2\n")
        payload = self.stop()
        self.assertBlocked(payload, "no prove-it run")
        self.assertIn(str(stop_hook.VERIFIER), payload["reason"])  # exact command to run

    def test_fresh_honest_run_allows_stop(self):
        self.write("app.py", "def old():\n    return 2\n")
        self.prove()
        self.assertIsNone(self.stop(HONEST))

    def test_edit_after_run_is_blocked(self):
        self.write("app.py", "def old():\n    return 2\n")
        self.prove()
        self.write("app.py", "def old():\n    return 3\n")
        self.assertBlocked(self.stop(HONEST), "changed after")

    def test_new_untracked_file_after_run_is_blocked(self):
        self.write("app.py", "def old():\n    return 2\n")
        self.prove()
        self.write("extra.py", "y = 1\n")
        self.assertBlocked(self.stop(HONEST), "changed after")

    def test_run_with_no_log_is_not_evidence(self):
        self.write("app.py", "def old():\n    return 2\n")
        path = self.write(".prove-it/claims.json", json.dumps([{"claim": "x", "files": ["app.py"]}]))
        with contextlib.redirect_stdout(io.StringIO()):
            prove_it.main(["--repo", str(self.repo), "--claims", str(path), "--no-log"])
        self.assertBlocked(self.stop(HONEST), "no prove-it run")

    def test_claims_file_in_repo_root_does_not_make_run_stale(self):
        self.write("app.py", "def old():\n    return 2\n")
        self.prove(claims_path="claims.json")
        self.assertIsNone(self.stop(HONEST))

    def test_old_log_entry_without_snapshot_is_rejected(self):
        self.write("app.py", "def old():\n    return 2\n")
        log = self.write(".prove-it/log.jsonl", json.dumps({"state": "VERIFIED", "fingerprint": "x"}) + "\n")
        self.assertTrue(log.exists())
        self.assertBlocked(self.stop(HONEST), "older version")

    # -- loopholes -----------------------------------------------------------

    def test_commit_during_turn_needs_a_run(self):
        self.turn_start()
        self.write("app.py", "def old():\n    return 2\n")
        self.commit("sneaky")
        payload = self.stop("Done, committed the fix.")
        self.assertBlocked(payload, "no prove-it run")
        self.assertIn("--base", payload["reason"])

    def test_commit_during_turn_with_run_against_head_is_blocked(self):
        self.turn_start()
        self.write("app.py", "def old():\n    return 2\n")
        self.commit("change")
        self.prove([{"claim": "nothing", "files": []}])  # base HEAD sees no changes
        self.assertBlocked(self.stop(HONEST), "commits were made this turn")

    def test_commit_during_turn_with_run_against_turn_start_is_allowed(self):
        self.turn_start()
        start = prove_it.rev_sha(self.repo, "HEAD")
        self.write("app.py", "def old():\n    return 2\n")
        self.commit("change")
        self.assertEqual(self.prove(None, ".prove-it/claims.json", "--base", start), 0)
        self.assertIsNone(self.stop(HONEST))

    def test_commits_without_turn_start_hook_are_not_checked(self):
        self.write("app.py", "def old():\n    return 2\n")
        self.commit("change")
        self.assertIsNone(self.stop())

    def test_unclaimed_changed_file_is_blocked(self):
        self.write("app.py", "def old():\n    return 2\n")
        self.write("other.py", "z = 1\n")
        self.prove()
        self.assertBlocked(self.stop(HONEST), "not covered by any claim: other.py")

    def test_run_with_no_claims_is_blocked(self):
        self.write("app.py", "def old():\n    return 2\n")
        path = self.write(".prove-it/claims.json", "[]")
        with contextlib.redirect_stdout(io.StringIO()):
            prove_it.main(["--repo", str(self.repo), "--claims", str(path)])
        self.assertBlocked(self.stop(HONEST), "no claims")

    def test_partial_run_must_say_what_is_not_verified(self):
        self.write("app.py", "def old():\n    return 2\n")
        self.prove()
        self.assertBlocked(self.stop("All done, it works."), "PARTIAL")
        self.assertIsNone(self.stop("Done. I could not verify the tests because they are disabled."))

    def test_failed_run_must_be_reported(self):
        self.write("app.py", "def old():\n    return 2\n")
        self.assertEqual(self.prove([{"claim": "changed both", "files": ["app.py", "other.py"]}]), 1)
        self.assertBlocked(self.stop("Done, everything works."), "FAILED")
        self.assertIsNone(self.stop("Done, but the claim failed: other.py was not changed."))

    def test_softer_done_wording_is_caught(self):
        self.write("app.py", "def old():\n    return 2\n")
        for message in ("All set!", "Good to go.", "I updated app.py.", "Should be good now."):
            self.assertBlocked(self.stop(message, session=message), "no prove-it run")

    # -- modes and limits ----------------------------------------------------

    def test_question_without_claim_words_is_allowed_in_claims_mode(self):
        self.write("app.py", "def old():\n    return 2\n")
        self.assertIsNone(self.stop("Which database should I use for this?"))

    def test_always_mode_checks_every_stop(self):
        self.config(mode="always")
        self.write("app.py", "def old():\n    return 2\n")
        self.assertBlocked(self.stop("Which database should I use for this?"), "no prove-it run")

    def test_off_mode_never_blocks(self):
        self.config(mode="off")
        self.write("app.py", "def old():\n    return 2\n")
        self.assertIsNone(self.stop())

    def test_block_limit_lets_the_session_end(self):
        self.write("app.py", "def old():\n    return 2\n")
        self.assertBlocked(self.stop(), "no prove-it run")
        self.assertBlocked(self.stop(), "no prove-it run")
        payload = self.stop()
        self.assertNotIn("decision", payload)
        self.assertIn("unverified", payload["systemMessage"])
        self.assertBlocked(self.stop(), "no prove-it run")  # counter starts over

    def test_block_count_is_per_session(self):
        self.config(max_blocks=1)
        self.write("app.py", "def old():\n    return 2\n")
        self.assertBlocked(self.stop(session="a"), "no prove-it run")
        self.assertBlocked(self.stop(session="b"), "no prove-it run")

    def test_new_turn_resets_block_count(self):
        self.config(max_blocks=1)
        self.write("app.py", "def old():\n    return 2\n")
        self.assertBlocked(self.stop(), "no prove-it run")
        self.turn_start()
        self.assertBlocked(self.stop(), "no prove-it run")

    # -- input handling ------------------------------------------------------

    def test_message_read_from_transcript(self):
        lines = [
            {"type": "user", "message": {"role": "user", "content": "fix it"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Done, it works."},
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
            ]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
            ]}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Should I also update the docs?"},
            ]}},
        ]
        transcript = self.write("transcript.jsonl", "\n".join(json.dumps(x) for x in lines))
        event = {"cwd": str(self.repo), "session_id": "s", "transcript_path": str(transcript)}
        self.assertEqual(stop_hook.last_assistant_text(event), "Should I also update the docs?")

    def test_not_a_git_repo_allows_stop(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.assertIsNone(stop_hook.decide({"cwd": tmp.name, "last_assistant_message": "Done."}))

    def test_main_prints_block_json(self):
        self.write("app.py", "def old():\n    return 2\n")
        out = self.main_with({"cwd": str(self.repo), "session_id": "m", "last_assistant_message": "Fixed."})
        self.assertEqual(json.loads(out)["decision"], "block")

    def test_main_user_prompt_submit_records_head_and_prints_nothing(self):
        out = self.main_with({"hook_event_name": "UserPromptSubmit", "cwd": str(self.repo), "session_id": "u"})
        self.assertEqual(out, "")
        state = json.loads((self.repo / ".prove-it" / "hook_state.json").read_text())
        self.assertEqual(state["turn_start"]["u"], prove_it.rev_sha(self.repo, "HEAD"))

    def test_main_ignores_bad_input(self):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("not json")), contextlib.redirect_stdout(out):
            self.assertEqual(stop_hook.main(), 0)
        self.assertEqual(out.getvalue(), "")

    # -- helpers -------------------------------------------------------------

    def commit(self, message):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)

    def main_with(self, event):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(event))), contextlib.redirect_stdout(out):
            self.assertEqual(stop_hook.main(), 0)
        return out.getvalue()


if __name__ == "__main__":
    unittest.main()
