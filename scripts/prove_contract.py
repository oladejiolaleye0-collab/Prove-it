"""Prove it contracts: what the agent commits to before it edits anything.

A contract lives at .prove-it/contract.json:

    {
      "task": "Reject negative prices",
      "scope": ["shop.py", "tests/test_shop.py"],
      "criteria": [
        {"id": "c1", "text": "price(-1) raises ValueError",
         "command": "python -m pytest tests/test_shop.py -k negative",
         "fails_before": true}
      ],
      "amendments": [{"scope": ["docs/**"], "reason": "the docs describe price rules"}],
      "allow_test_changes": [{"file": "tests/test_old.py", "reason": "the old behavior is removed"}]
    }

Pure functions only (no git, no subprocess), shared by prove_it.py and the hook.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

CONTRACT_FILE = ".prove-it/contract.json"
DEFAULT_TEST_PATTERNS = (
    "test_*.py", "*_test.py", "**/test_*.py", "**/*_test.py",
    "tests/**", "test/**", "**/tests/**", "**/test/**", "**/__tests__/**",
    "*.test.*", "*.spec.*", "**/*.test.*", "**/*.spec.*",
    "*_test.go", "**/*_test.go", "spec/**", "**/spec/**",
)
# Lines that check something. Counted, not parsed: a net drop in a test file
# means checks were removed.
ASSERT_RE = re.compile(
    r"\bassert\w*\b|\bexpect\s*\(|\.should\b|\bt\.(Error|Errorf|Fatal|Fatalf|Fail)\b|"
    r"\bassert(_eq|_ne)?!\s*\(|\.to(Be|Equal|Throw|Have|Match|Contain)\w*\s*\(|\bpytest\.raises\b"
)
# Markers that switch tests off or narrow a run to a few tests.
SKIP_RE = re.compile(
    r"@pytest\.mark\.(skip|skipif|xfail)\b|@unittest\.(skip|skipIf|skipUnless|expectedFailure)\b|"
    r"\bpytest\.skip\s*\(|\bself\.skipTest\s*\(|\b(it|test|describe|context)\.(skip|only|todo)\s*\(|"
    r"\b(xit|xdescribe|xtest|fit|fdescribe)\s*\(|\bt\.Skip(f|Now)?\s*\(|#\[ignore\]"
)


class ContractError(Exception):
    """The contract is missing a field or has the wrong shape."""


def _str_list(value, where: str, allow_empty: bool = True) -> list:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ContractError(f"{where} must be a list of non-empty strings")
    if not allow_empty and not value:
        raise ContractError(f"{where} must not be empty")
    return [v.strip() for v in value]


def _command_ok(value) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return isinstance(value, list) and bool(value) and all(isinstance(v, str) for v in value)


def parse_contract(data) -> dict:
    """Validate a contract and return it in normal form. Raises ContractError."""
    if not isinstance(data, dict):
        raise ContractError("the contract must be a JSON object")
    task = data.get("task")
    if not isinstance(task, str) or not task.strip():
        raise ContractError("'task' must be a non-empty string")
    scope = _str_list(data.get("scope"), "'scope'", allow_empty=False)

    raw = data.get("criteria")
    if not isinstance(raw, list) or not raw:
        raise ContractError("'criteria' must be a non-empty list")
    criteria, seen = [], set()
    for index, item in enumerate(raw, 1):
        where = f"criterion #{index}"
        if not isinstance(item, dict):
            raise ContractError(f"{where} must be an object")
        cid = item.get("id")
        if not isinstance(cid, str) or not cid.strip():
            raise ContractError(f"{where} needs a non-empty string 'id'")
        if cid in seen:
            raise ContractError(f"criterion id '{cid}' is used twice")
        seen.add(cid)
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ContractError(f"criterion '{cid}' needs a non-empty string 'text'")
        criterion = {"id": cid.strip(), "text": text.strip()}
        if "command" in item:
            if not _command_ok(item["command"]):
                raise ContractError(f"criterion '{cid}': 'command' must be a non-empty string or list of strings")
            criterion["command"] = item["command"]
        fails_before = item.get("fails_before", False)
        if not isinstance(fails_before, bool):
            raise ContractError(f"criterion '{cid}': 'fails_before' must be true or false")
        if fails_before and "command" not in criterion:
            raise ContractError(f"criterion '{cid}': 'fails_before' needs a 'command'")
        criterion["fails_before"] = fails_before
        if "tests" in item:
            criterion["tests"] = _str_list(item["tests"], f"criterion '{cid}': 'tests'")
        criteria.append(criterion)

    amendments = []
    for index, item in enumerate(data.get("amendments") or [], 1):
        if not isinstance(item, dict):
            raise ContractError(f"amendment #{index} must be an object")
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ContractError(f"amendment #{index} needs a 'reason'")
        amendments.append({
            "scope": _str_list(item.get("scope"), f"amendment #{index}: 'scope'", allow_empty=False),
            "reason": reason.strip(),
        })

    allowed = []
    for index, item in enumerate(data.get("allow_test_changes") or [], 1):
        if not isinstance(item, dict) or not isinstance(item.get("file"), str) or not item["file"].strip():
            raise ContractError(f"allow_test_changes #{index} needs a 'file'")
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ContractError(f"allow_test_changes #{index} needs a 'reason'")
        allowed.append({"file": normalize(item["file"]), "reason": reason.strip()})

    return {
        "task": task.strip(),
        "scope": scope,
        "criteria": criteria,
        "amendments": amendments,
        "allow_test_changes": allowed,
    }


def load_contract(path: Path):
    """Return the parsed contract, or None if the file does not exist."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ContractError(f"cannot read {path.name}: {exc}")
    except ValueError as exc:
        raise ContractError(f"{path.name} is not valid JSON: {exc}")
    return parse_contract(data)


def contract_hash(contract: dict) -> str:
    return hashlib.sha256(json.dumps(contract, sort_keys=True).encode("utf-8")).hexdigest()


def normalize(path: str) -> str:
    path = path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _glob_regex(pattern: str):
    pattern = normalize(pattern)
    if pattern.endswith("/"):
        pattern += "**"
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def glob_match(path: str, pattern: str) -> bool:
    """Match a repo-relative path. '*' stays within a folder, '**' crosses
    folders, and a pattern ending in '/' matches everything under it."""
    return bool(_glob_regex(pattern).match(normalize(path)))


def scope_patterns(contract: dict) -> list:
    patterns = list(contract["scope"])
    for amendment in contract["amendments"]:
        patterns.extend(amendment["scope"])
    return patterns


def in_scope(path: str, contract: dict) -> bool:
    path = normalize(path)
    if path.startswith(".prove-it/"):
        return True
    return any(glob_match(path, pattern) for pattern in scope_patterns(contract))


def is_test_file(path: str, patterns=DEFAULT_TEST_PATTERNS) -> bool:
    return any(glob_match(path, pattern) for pattern in patterns)


def dropped_since(locked: dict, current: dict) -> list:
    """What the current contract takes back compared with the locked one."""
    problems = []
    now = {c["id"]: c for c in current["criteria"]}
    for criterion in locked["criteria"]:
        later = now.get(criterion["id"])
        if later is None:
            problems.append(f"criterion '{criterion['id']}' was removed")
        elif criterion.get("fails_before") and not later.get("fails_before"):
            problems.append(f"criterion '{criterion['id']}' no longer requires fails_before")
    kept = {json.dumps(a, sort_keys=True) for a in current["amendments"]}
    for amendment in locked["amendments"]:
        if json.dumps(amendment, sort_keys=True) not in kept:
            problems.append(f"amendment '{amendment['reason']}' was removed")
    return problems


def test_integrity(files: dict, added: dict, removed: dict, patterns=DEFAULT_TEST_PATTERNS) -> list:
    """Return [(path, problem)] for test files the diff weakens."""
    findings = []
    for path in sorted(files):
        if not is_test_file(path, patterns):
            continue
        if files[path] == "D":
            findings.append((path, "test file deleted"))
            continue
        plus, minus = added.get(path, []), removed.get(path, [])
        lost = sum(1 for line in minus if ASSERT_RE.search(line)) - sum(1 for line in plus if ASSERT_RE.search(line))
        if lost > 0:
            findings.append((path, f"{lost} fewer assertion line(s) than before"))
        new_skips = [line.strip() for line in plus if SKIP_RE.search(line)]
        if len(new_skips) > sum(1 for line in minus if SKIP_RE.search(line)):
            findings.append((path, f"adds a skip or only marker: {new_skips[0][:80]}"))
    return findings
