#!/usr/bin/env python3
"""Prove it hook for Claude Code.

Register this one script for three events:
  UserPromptSubmit  records HEAD when a turn starts (so commits made during
                    the turn are caught) and reminds Claude of the protocol.
  PreToolUse        keeps Claude on course: no edits before a contract, no
                    edits outside the contract's scope, no dropping criteria
                    mid-turn, no tampering with Prove it's own files.
  Stop              blocks Claude from ending its turn with a "done" report
                    unless a logged prove-it run backs it.

Reads the hook event as JSON on stdin and prints a decision as JSON on
stdout. Standard library only.

Settings live under "hook" in the project's .prove-it.json:
    {"hook": {"mode": "claims", "max_blocks": 2, "contract": "required"}}
mode: "claims" (default) checks only when the last message sounds like a
report of finished work, "always" checks every stop with changes, "off"
disables the hook. contract: "required" (default) blocks edits until a
contract exists, "optional" enforces scope only when one exists, "off"
ignores contracts.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prove_contract  # noqa: E402
import prove_it  # noqa: E402

MODES = ("claims", "always", "off")
CONTRACT_MODES = ("required", "optional", "off")
DEFAULT_SETTINGS = {"mode": "claims", "max_blocks": 2, "contract": "required"}
STATE_FILE = "hook_state.json"
STATE_KEYS = ("turn_start", "blocks", "locked")
MAX_SESSIONS_KEPT = 20
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
# Files the agent must never change: they decide what gets checked.
PROTECTED = (
    ".prove-it.json", ".prove-it/log.jsonl", ".prove-it/hook_state.json",
    ".claude/settings.json", ".claude/settings.local.json",
)
PROTECTED_IN_SHELL = re.compile(
    r"log\.jsonl|hook_state\.json|\.prove-it\.json|\.claude[/\\]+settings(\.local)?\.json"
)
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
CONTRACT_EXAMPLE = (
    '{"task": "...", "scope": ["path/or/glob/**"], "criteria": [{"id": "c1", '
    '"text": "what must be true", "command": "a test command that proves it", '
    '"fails_before": true}]}'
)
HOW_TO = (
    "Before you say the work is done: make sure .prove-it/contract.json describes the "
    "task (or, without a contract, write claims to .prove-it/claims.json listing every "
    "changed file), run `{command}` from the project root, and report each criterion "
    "with its evidence, plus Not verified and Failed, from its output. If the work is "
    "not finished, say what is left instead of claiming it is done."
)


def verifier_command(base: str = "") -> str:
    command = f'"{sys.executable}" "{VERIFIER}"'
    if base:
        command += f" --base {base}"
    return command


def how_to(base: str = "", claims: bool = False) -> str:
    command = verifier_command(base)
    if claims:
        command += " --claims .prove-it/claims.json"
    return HOW_TO.format(command=command)


def load_settings(repo: Path) -> dict:
    settings = dict(DEFAULT_SETTINGS)
    config = prove_it._read_json(repo / ".prove-it.json")
    hook = config.get("hook") if isinstance(config, dict) else None
    if isinstance(hook, dict):
        if hook.get("mode") in MODES:
            settings["mode"] = hook["mode"]
        if hook.get("contract") in CONTRACT_MODES:
            settings["contract"] = hook["contract"]
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


def _is_git(repo: Path) -> bool:
    return prove_it._resolve_ref(repo, "HEAD")[0] is not None


def _load_contract(repo: Path):
    """Return (contract or None, error or None)."""
    try:
        return prove_contract.load_contract(repo / prove_contract.CONTRACT_FILE), None
    except prove_contract.ContractError as exc:
        return None, str(exc)


# --------------------------------------------------------------------------
# State: per session HEAD at turn start, consecutive blocks, locked contract
# --------------------------------------------------------------------------


def _read_state(repo: Path) -> dict:
    state = prove_it._read_json(repo / prove_it.LOG_DIR / STATE_FILE)
    if not isinstance(state, dict):
        state = {}
    for key in STATE_KEYS:
        if not isinstance(state.get(key), dict):
            state[key] = {}
    return state


def _write_state(repo: Path, state: dict) -> None:
    for key in STATE_KEYS:
        state[key] = dict(list(state[key].items())[-MAX_SESSIONS_KEPT:])
    directory = repo / prove_it.LOG_DIR
    try:
        directory.mkdir(parents=True, exist_ok=True)
        ignore = directory / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
        (directory / STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def record_turn_start(event: dict):
    """Record HEAD for this turn and return context reminding Claude of the protocol."""
    repo = Path(event.get("cwd") or ".").resolve()
    if not repo.is_dir():
        return None
    settings = load_settings(repo)
    if settings["mode"] == "off" or not _is_git(repo):
        return None
    session = str(event.get("session_id") or "")
    state = _read_state(repo)
    state["turn_start"].pop(session, None)  # re-insert so it counts as recent
    state["turn_start"][session] = prove_it.rev_sha(repo, "HEAD") or ""
    state["blocks"].pop(session, None)
    state["locked"].pop(session, None)
    _write_state(repo, state)
    if settings["contract"] == "off":
        return None
    first = ("Before editing any file, write" if settings["contract"] == "required"
             else "Before editing files, you may write")
    context = (
        f"Prove it is active in this project. {first} .prove-it/contract.json "
        f"(replace it if it describes an earlier task): {CONTRACT_EXAMPLE}. "
        "Use fails_before for a test that should fail without your change. Edits outside "
        'the scope are blocked; to widen it, add {"amendments": [{"scope": [...], '
        '"reason": "..."}]} and mention it in your report. Criteria cannot be dropped once '
        f"you start editing. Before saying you are done, run `{verifier_command()}` and "
        "report its result."
    )
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}


# --------------------------------------------------------------------------
# PreToolUse: stay on course
# --------------------------------------------------------------------------


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": f"Prove it: {reason}",
    }}


def guard(event: dict):
    """Return a deny payload for an off-course tool call, or None to allow it."""
    repo = Path(event.get("cwd") or ".").resolve()
    if not repo.is_dir():
        return None
    settings = load_settings(repo)
    if settings["mode"] == "off":
        return None
    tool = event.get("tool_name") or ""
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}

    if tool == "Bash":
        command = tool_input.get("command") or ""
        if PROTECTED_IN_SHELL.search(command) and "prove_it.py" not in command:
            return _deny("this command touches Prove it's settings or records, which only "
                         "the user may change. Ask the user if it is really needed.")
        return None
    if tool not in EDIT_TOOLS:
        return None

    raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not raw:
        return None
    target = Path(raw)
    if not target.is_absolute():
        target = repo / target
    try:
        rel = prove_contract.normalize(str(target.resolve().relative_to(repo)))
    except (ValueError, OSError):
        return None  # outside the project: not part of the diff
    if not _is_git(repo):
        return None
    if rel in PROTECTED:
        return _deny(f"{rel} decides what gets checked; only the user may change it.")

    session = str(event.get("session_id") or "")
    state = _read_state(repo)
    locked = state["locked"].get(session)

    if rel == prove_contract.CONTRACT_FILE:
        if tool == "Write":
            try:
                new = prove_contract.parse_contract(json.loads(tool_input.get("content") or ""))
            except ValueError as exc:
                return _deny(f"the contract is not valid JSON: {exc}")
            except prove_contract.ContractError as exc:
                return _deny(f"the contract is not valid: {exc}. Example: {CONTRACT_EXAMPLE}")
            if locked:
                taken_back = prove_contract.dropped_since(locked, new)
                if taken_back:
                    return _deny("you started editing under this contract, so it cannot be weakened "
                                 "now: " + "; ".join(taken_back) + ". Keep the criteria and report "
                                 "any you could not meet as not done.")
        return None
    if rel.startswith(prove_it.LOG_DIR + "/"):
        return None  # claims and other scratch files
    if settings["contract"] == "off":
        return None

    contract, error = _load_contract(repo)
    if error:
        return _deny(f".prove-it/contract.json is not valid: {error}. Fix it before editing.")
    if contract is None:
        if settings["contract"] == "required":
            return _deny("write .prove-it/contract.json before editing files, so the work can be "
                         f"checked against what you said you would do. Format: {CONTRACT_EXAMPLE}")
        return None
    if not prove_contract.in_scope(rel, contract):
        return _deny(
            f"{rel} is outside the contract's scope ({', '.join(prove_contract.scope_patterns(contract))}). "
            "If this change is really needed for the task, add it to .prove-it/contract.json as "
            '{"amendments": [{"scope": ["' + rel + '"], "reason": "why it is needed"}]} and say so '
            "in your report. Otherwise stay within the scope."
        )
    if not locked:
        state["locked"][session] = contract
        _write_state(repo, state)
    return None


# --------------------------------------------------------------------------
# Stop decision
# --------------------------------------------------------------------------


def evidence_problem(repo: Path, entry, text, head, fingerprint, turn_start, contract):
    """Return (why the latest run does not back this stop, base hint), or None."""
    moved = bool(turn_start) and head != turn_start
    hint = turn_start if moved else ""
    if entry is None:
        return "there are changes and no prove-it run in this project", hint
    if not entry.get("fingerprint") or "head" not in entry:
        return "the last prove-it run is from an older version or had logging off; run it again", hint
    if entry.get("head") != head or entry["fingerprint"] != fingerprint:
        return "files or commits changed after the last prove-it run", hint
    if contract is not None and entry.get("contract_hash") != prove_contract.contract_hash(contract):
        return "the last prove-it run did not check the current contract; run it again", hint
    if moved:
        base_sha = entry.get("base_sha")
        if not base_sha or not prove_it.is_ancestor(repo, base_sha, turn_start):
            return (
                f"commits were made this turn, but the last run compared against "
                f"'{entry.get('base')}', which does not include them", hint
            )
    if contract is None:
        if not entry.get("claims"):
            return "the last prove-it run had no claims and there is no contract", hint
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
    session = str(event.get("session_id") or "")
    state = _read_state(repo)
    locked = state["locked"].get(session) if settings["contract"] != "off" else None

    # Cheapest test first: in claims mode most non-claims never touch git.
    # Work done under a contract this turn is always checked.
    text = last_assistant_text(event)
    if (settings["mode"] == "claims" and not locked and text is not None
            and not CLAIM_WORDS.search(text)):
        return None

    entry = latest_run(repo)
    ignore = frozenset((entry or {}).get("ignore") or [])
    snapshot = prove_it.working_changes(repo, "HEAD", ignore)
    if snapshot is None:
        return None  # not a git repo
    head = prove_it.rev_sha(repo, "HEAD")
    turn_start = state["turn_start"].get(session) or ""
    if not snapshot[0] and not (turn_start and head != turn_start):
        return None  # nothing uncommitted and no commits this turn

    contract, error = (None, None) if settings["contract"] == "off" else _load_contract(repo)
    found = None
    if error:
        found = (f".prove-it/contract.json is not valid: {error}", "")
    elif locked and contract is None:
        found = ("the contract you worked under this turn was deleted; restore it", "")
    elif locked and prove_contract.dropped_since(locked, contract):
        found = ("the contract was weakened after you started editing: "
                 + "; ".join(prove_contract.dropped_since(locked, contract))
                 + ". Restore it and report unmet criteria as not done", "")
    else:
        found = evidence_problem(repo, entry, text, head, snapshot[1], turn_start, contract)
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
    reason = f"Prove it: {problem}. {how_to(base_hint, claims=contract is None)}"
    return {"decision": "block", "reason": reason}


def main() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0  # not a hook event; never break the session over bad input
    if not isinstance(event, dict):
        return 0
    name = event.get("hook_event_name")
    try:
        if name == "UserPromptSubmit":
            payload = record_turn_start(event)
        elif name == "PreToolUse":
            payload = guard(event)
        else:
            payload = decide(event)
    except Exception as exc:  # a broken hook must not trap the session
        if name == "PreToolUse":
            return 0
        payload = {"systemMessage": f"Prove it hook error, stop allowed: {exc}"}
    if payload:
        print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
