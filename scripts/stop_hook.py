#!/usr/bin/env python3
"""Prove it Stop hook for Claude Code.

Blocks Claude from ending its turn with a "done" claim while the working tree
has changes that no prove-it run covers. Reads the hook event as JSON on
stdin and prints a decision as JSON on stdout. Standard library only.

Settings live under "hook" in the project's .prove-it.json:
    {"hook": {"mode": "claims", "max_blocks": 2}}
mode: "claims" (default) checks only when the last message sounds like a done
claim, "always" checks every stop with changes, "off" disables the hook.
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
CLAIM_WORDS = re.compile(
    r"\b(done|fixed|finished|complete|completed|implemented|works|working|"
    r"pass|passes|passed|passing|ready|resolved|verified)\b",
    re.IGNORECASE,
)
MENTIONS_FAILURE = re.compile(r"\bfail", re.IGNORECASE)
HOW_TO = (
    "Before you say the work is done: write your claims to .prove-it/claims.json, "
    "run prove_it.py --claims .prove-it/claims.json from the project root, and report "
    "Verified / Not verified / Failed from its output. If the work is not finished, "
    "say what is left instead of claiming it is done."
)


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


def evidence_problem(repo: Path, entry, text):
    """Return why the latest run does not back this stop, or None if it does."""
    if entry is None:
        return "there are uncommitted changes and no prove-it run in this project"
    if not entry.get("fingerprint"):
        return "the last prove-it run has no fingerprint (run it again with logging on)"
    ignore = frozenset(entry.get("ignore") or [])
    current = prove_it.working_changes(repo, entry.get("base") or "HEAD", ignore)
    if current is None:
        return f"cannot compare files against the last run's base '{entry.get('base')}'"
    if current[1] != entry["fingerprint"]:
        return "files changed after the last prove-it run"
    if entry.get("state") == "FAILED" and text is not None and not MENTIONS_FAILURE.search(text):
        return "the last prove-it run FAILED but your message does not say what failed"
    return None


def _read_state(repo: Path) -> dict:
    state = prove_it._read_json(repo / prove_it.LOG_DIR / STATE_FILE)
    return state if isinstance(state, dict) else {}


def _write_state(repo: Path, state: dict) -> None:
    directory = repo / prove_it.LOG_DIR
    try:
        directory.mkdir(parents=True, exist_ok=True)
        ignore = directory / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
        (directory / STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def decide(event: dict):
    """Return the JSON payload to print, or None to let Claude stop."""
    repo = Path(event.get("cwd") or ".").resolve()
    if not repo.is_dir():
        return None
    settings = load_settings(repo)
    if settings["mode"] == "off":
        return None

    entry = latest_run(repo)
    ignore = frozenset((entry or {}).get("ignore") or [])
    changes = prove_it.working_changes(repo, "HEAD", ignore)
    if changes is None or not changes[0]:
        return None  # no git, or nothing uncommitted to vouch for

    text = last_assistant_text(event)
    if settings["mode"] == "claims" and text is not None and not CLAIM_WORDS.search(text):
        return None

    session = str(event.get("session_id") or "")
    problem = evidence_problem(repo, entry, text)
    if problem is None:
        if _read_state(repo).get("blocks"):
            _write_state(repo, {"session": session, "blocks": 0})
        return None

    state = _read_state(repo)
    blocks = state.get("blocks", 0) if state.get("session") == session else 0
    if not isinstance(blocks, int):
        blocks = 0
    if blocks >= settings["max_blocks"]:
        _write_state(repo, {"session": session, "blocks": 0})
        return {
            "systemMessage": f"Prove it: {problem}. Allowed the stop after "
                             f"{blocks} block(s); treat the work as unverified."
        }
    _write_state(repo, {"session": session, "blocks": blocks + 1})
    return {"decision": "block", "reason": f"Prove it: {problem}. {HOW_TO}"}


def main() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0  # not a hook event; never break the session over bad input
    if not isinstance(event, dict):
        return 0
    try:
        payload = decide(event)
    except Exception as exc:  # a broken hook must not trap the session
        payload = {"systemMessage": f"Prove it hook error, stop allowed: {exc}"}
    if payload:
        print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
