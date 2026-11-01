#!/usr/bin/env python3
"""Prove it hook for Claude Code.

Register the same command for two events:
  UserPromptSubmit  records HEAD when a turn starts, so commits made during
                    the turn are caught.
  Stop              blocks Claude from ending its turn with a "done" claim
                    unless a logged prove-it run covers the current files.

Reads the hook event as JSON on stdin and prints a decision as JSON on
stdout. Standard library only.

Settings live under "hook" in the project's .prove-it.json:
    {"hook": {"mode": "claims", "max_blocks": 2}}
mode: "claims" (default) checks only when the last message sounds like a
report of finished work, "always" checks every stop with changes, "off"
disables the hook.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prove_it  # noqa: E402

MODES = ("claims", "always", "off")
DEFAULT_SETTINGS = {"mode": "claims", "max_blocks": 2}
STATE_FILE = "hook_state.json"
MAX_SESSIONS_KEPT = 20
CLAIM_WORDS = re.compile(
    r"\b(done|fixed|fixes|finished|complete|completed|implemented|works|working|"
    r"pass|passes|passed|passing|ready|resolved|verified|added|updated|changed|"
    r"created|wrote|written|refactored|removed|renamed|deleted|moved|replaced|"
    r"committed|pushed|merged|shipped|lgtm|all set|good to go|should be good)\b",
    re.IGNORECASE,
)
MENTIONS_FAILURE = re.compile(r"\bfail", re.IGNORECASE)
MENTIONS_GAPS = re.compile(
    r"not verified|unverified|could ?n[o']t verify|not checked|could ?n[o']t check",
    re.IGNORECASE,
)
VERIFIER = Path(__file__).resolve().parent / "prove_it.py"
HOW_TO = (
    "Before you say the work is done: write your claims to .prove-it/claims.json, "
    "with every changed file listed in some claim's \"files\", run `{command}` from "
    "the project root, and report Verified / Not verified / Failed from its output. "
    "If the work is not finished, say what is left instead of claiming it is done."
)


def how_to(base: str = "") -> str:
    command = f'"{sys.executable}" "{VERIFIER}" --claims .prove-it/claims.json'
    if base:
        command += f" --base {base}"
    return HOW_TO.format(command=command)


def load_settings(repo: Path) -> dict:
    settings = dict(DEFAULT_SETTINGS)
    config = prove_it._read_json(repo / ".prove-it.json")
    hook = config.get("hook") if isinstance(config, dict) else None
    if isinstance(hook, dict):
        if hook.get("mode") in MODES:
            settings["mode"] = hook["mode"]
        max_blocks = hook.get("max_blocks")
        if isinstance(max_blocks, int) and not isinstance(max_blocks, bool) and max_blocks >= 0:
            settings["max_blocks"] = max_blocks
    return settings


def latest_run(repo: Path):
    lines = prove_it._read_text(repo / prove_it.LOG_DIR / prove_it.LOG_FILE).splitlines()
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            return entry
    return None


def _text_blocks(content) -> list:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]


def last_assistant_text(event: dict):
    """Text Claude wrote after its last tool call, or None if unknown."""
    if isinstance(event.get("last_assistant_message"), str):
        return event["last_assistant_message"]
    path = event.get("transcript_path")
    if not isinstance(path, str) or not path:
        return None
    raw = prove_it._read_text(Path(path).expanduser())
    if not raw:
        return None
    texts: list = []
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        content = message.get("content")
        if entry.get("type") == "user":
            texts = []  # a new prompt or a tool result starts a new stretch
        elif entry.get("type") == "assistant":
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_use" for b in content
            ):
                texts = []
            texts.extend(_text_blocks(content))
    return "\n".join(t for t in texts if t)


# --------------------------------------------------------------------------
# State: HEAD at turn start and consecutive blocks, per session
# --------------------------------------------------------------------------


def _read_state(repo: Path) -> dict:
    state = prove_it._read_json(repo / prove_it.LOG_DIR / STATE_FILE)
    if not isinstance(state, dict):
        return {"turn_start": {}, "blocks": {}}
    for key in ("turn_start", "blocks"):
        if not isinstance(state.get(key), dict):
            state[key] = {}
    return state


def _write_state(repo: Path, state: dict) -> None:
    for key in ("turn_start", "blocks"):
        items = list(state[key].items())[-MAX_SESSIONS_KEPT:]
        state[key] = dict(items)
    directory = repo / prove_it.LOG_DIR
    try:
        directory.mkdir(parents=True, exist_ok=True)
        ignore = directory / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
        (directory / STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def record_turn_start(event: dict) -> None:
    repo = Path(event.get("cwd") or ".").resolve()
    if not repo.is_dir() or load_settings(repo)["mode"] == "off":
        return
    if prove_it.working_changes(repo, "HEAD") is None:
        return  # not a git repo
    session = str(event.get("session_id") or "")
    state = _read_state(repo)
    state["turn_start"].pop(session, None)  # re-insert so it counts as recent
    state["turn_start"][session] = prove_it.rev_sha(repo, "HEAD") or ""
    state["blocks"].pop(session, None)
    _write_state(repo, state)


# --------------------------------------------------------------------------
# Stop decision
# --------------------------------------------------------------------------


def evidence_problem(repo: Path, entry, text, head, fingerprint, turn_start):
    """Return (why the latest run does not back this stop, base hint), or None."""
    moved = bool(turn_start) and head != turn_start
    hint = turn_start if moved else ""
    if entry is None:
        return "there are changes and no prove-it run in this project", hint
    if not entry.get("fingerprint") or "head" not in entry:
        return "the last prove-it run is from an older version or had logging off; run it again", hint
    if entry.get("head") != head or entry["fingerprint"] != fingerprint:
        return "files or commits changed after the last prove-it run", hint
    if moved:
        base_sha = entry.get("base_sha")
        if not base_sha or not prove_it.is_ancestor(repo, base_sha, turn_start):
            return (
                f"commits were made this turn, but the last run compared against "
                f"'{entry.get('base')}', which does not include them", hint
            )
    if not entry.get("claims"):
        return "the last prove-it run had no claims", hint
    unclaimed = entry.get("unclaimed") or []
    if unclaimed:
        shown = ", ".join(unclaimed[:10]) + (" ..." if len(unclaimed) > 10 else "")
        return f"changed files are not covered by any claim: {shown}", hint
    if text is not None:
        if entry.get("state") == "FAILED" and not MENTIONS_FAILURE.search(text):
            return "the last prove-it run FAILED but your message does not say what failed", hint
        if entry.get("state") == "PARTIAL" and not MENTIONS_GAPS.search(text):
            return "the last prove-it run was PARTIAL but your message does not say what is not verified", hint
    return None


def decide(event: dict):
    """Return the JSON payload to print, or None to let Claude stop."""
    repo = Path(event.get("cwd") or ".").resolve()
    if not repo.is_dir():
        return None
    settings = load_settings(repo)
    if settings["mode"] == "off":
        return None

    # Cheapest test first: in claims mode most non-claims never touch git.
    text = last_assistant_text(event)
    if settings["mode"] == "claims" and text is not None and not CLAIM_WORDS.search(text):
        return None

    entry = latest_run(repo)
    ignore = frozenset((entry or {}).get("ignore") or [])
    snapshot = prove_it.working_changes(repo, "HEAD", ignore)
    if snapshot is None:
        return None  # not a git repo
    head = prove_it.rev_sha(repo, "HEAD")
    session = str(event.get("session_id") or "")
    state = _read_state(repo)
    turn_start = state["turn_start"].get(session) or ""
    if not snapshot[0] and not (turn_start and head != turn_start):
        return None  # nothing uncommitted and no commits this turn

    found = evidence_problem(repo, entry, text, head, snapshot[1], turn_start)
    if found is None:
        if state["blocks"].pop(session, None):
            _write_state(repo, state)
        return None
    problem, base_hint = found

    blocks = state["blocks"].get(session, 0)
    if not isinstance(blocks, int):
        blocks = 0
    if blocks >= settings["max_blocks"]:
        state["blocks"].pop(session, None)
        _write_state(repo, state)
        return {
            "systemMessage": f"Prove it: {problem}. Allowed the stop after "
                             f"{blocks} block(s); treat the work as unverified."
        }
    state["blocks"][session] = blocks + 1
    _write_state(repo, state)
    return {"decision": "block", "reason": f"Prove it: {problem}. {how_to(base_hint)}"}


def main() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0  # not a hook event; never break the session over bad input
    if not isinstance(event, dict):
        return 0
    try:
        if event.get("hook_event_name") == "UserPromptSubmit":
            record_turn_start(event)  # prints nothing: its output would become context
            return 0
        payload = decide(event)
    except Exception as exc:  # a broken hook must not trap the session
        payload = {"systemMessage": f"Prove it hook error, stop allowed: {exc}"}
    if payload:
        print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
