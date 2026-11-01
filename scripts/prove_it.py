#!/usr/bin/env python3
"""Prove it: run the project's real checks and match an agent's claims
against the git diff. Standard library only (Python 3.8+).

Usage:
    python3 prove_it.py --claims claims.json
    python3 prove_it.py --history
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

TAIL_LINES = 30
PASS_TAIL_LINES = 3
MAX_UNTRACKED_BYTES = 1_000_000
MAX_UNCLAIMED_SHOWN = 15
ALL_CHECKS = ("test", "build", "lint")
CHECK_KINDS = {"tests_pass": "test", "build_ok": "build", "lint_ok": "lint"}
NPM_DEFAULT_TEST = "no test specified"
LOG_DIR = ".prove-it"
LOG_FILE = "log.jsonl"
STATUS_NAMES = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "T": "type changed",
    "?": "new, untracked",
}


class UsageError(Exception):
    """Bad input from the caller; reported without a traceback."""


@dataclass
class CheckResult:
    kind: str
    status: str  # PASS | FAIL | TIMEOUT | NOT_RUN | NOT_CONFIGURED
    command: str = ""
    exit_code: int | None = None
    seconds: float = 0.0
    output_tail: str = ""
    note: str = ""


@dataclass
class DiffInfo:
    available: bool
    base: str
    files: dict = field(default_factory=dict)  # path -> status letter
    added: dict = field(default_factory=dict)  # path -> list of added lines
    note: str = ""


@dataclass
class ClaimResult:
    claim: str
    kind: str
    verdict: str  # SUPPORTED | NOT_SUPPORTED | UNVERIFIED
    details: list = field(default_factory=list)  # [[verdict, text], ...]
    output_tail: str = ""  # output of the claim's own command when it did not pass


# --------------------------------------------------------------------------
# Command detection
# --------------------------------------------------------------------------


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _to_argv(value, windows: bool | None = None):
    if windows is None:
        windows = os.name == "nt"
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        return list(value)
    if isinstance(value, str) and value.strip():
        if not windows:
            return shlex.split(value)
        # Non-POSIX mode keeps backslashes in Windows paths, but also keeps
        # the quotes around each token, so strip those.
        argv = []
        for token in shlex.split(value, posix=False):
            if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
                token = token[1:-1]
            argv.append(token)
        return argv
    return None


def _find_exe(name: str, windows: bool | None = None):
    """Locate a command. On Windows, 'python' and 'python3' often resolve to a
    Microsoft Store placeholder that only prints an install hint, so use the
    Python running this script instead."""
    if windows is None:
        windows = os.name == "nt"
    exe = shutil.which(name)
    if windows and name.lower() in ("python", "python3", "python.exe", "python3.exe"):
        if exe is None or "windowsapps" in exe.replace("/", "\\").lower():
            return sys.executable
    return exe


def _detect_node(repo: Path) -> dict:
    pkg = _read_json(repo / "package.json")
    if not isinstance(pkg, dict) or not isinstance(pkg.get("scripts"), dict):
        return {}
    if (repo / "pnpm-lock.yaml").exists():
        runner = ["pnpm", "run"]
    elif (repo / "yarn.lock").exists():
        runner = ["yarn", "run"]
    elif (repo / "bun.lock").exists() or (repo / "bun.lockb").exists():
        runner = ["bun", "run"]
    else:
        runner = ["npm", "run"]
    found = {}
    for kind in ALL_CHECKS:
        body = pkg["scripts"].get(kind)
        if not isinstance(body, str):
            continue
        if kind == "test" and NPM_DEFAULT_TEST in body:
            continue  # the placeholder script npm generates; it always fails
        found[kind] = runner + [kind]
    return found


def _detect_python(repo: Path) -> dict:
    found = {}
    pyproject = _read_text(repo / "pyproject.toml")
    has_pytest = (
        any((repo / name).exists() for name in ("pytest.ini", "tox.ini", "conftest.py"))
        or "[tool.pytest" in pyproject
        or (repo / "tests").is_dir()
        or any(repo.glob("test_*.py"))
    )
    if has_pytest:
        found["test"] = [sys.executable, "-m", "pytest", "-q"]
    has_ruff = (
        (repo / "ruff.toml").exists()
        or (repo / ".ruff.toml").exists()
        or "[tool.ruff" in pyproject
    )
    if has_ruff:
        found["lint"] = ["ruff", "check", "."]
    return found


def _detect_go(repo: Path) -> dict:
    if not (repo / "go.mod").exists():
        return {}
    return {
        "test": ["go", "test", "./..."],
        "build": ["go", "build", "./..."],
        "lint": ["go", "vet", "./..."],
    }


def _detect_rust(repo: Path) -> dict:
    if not (repo / "Cargo.toml").exists():
        return {}
    return {"test": ["cargo", "test"], "build": ["cargo", "build"]}


def _detect_make(repo: Path) -> dict:
    text = _read_text(repo / "Makefile")
    if not text:
        return {}
    return {
        kind: ["make", kind]
        for kind in ALL_CHECKS
        if re.search(rf"^{kind}\s*:", text, re.MULTILINE)
    }


def detect_commands(repo: Path) -> tuple[dict, set]:
    """Return ({kind: argv}, disabled_kinds). Config beats auto-detection."""
    commands: dict = {}
    disabled: set = set()
    config_path = repo / ".prove-it.json"
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise UsageError(f"cannot read .prove-it.json: {exc}")
        if not isinstance(config, dict):
            raise UsageError(".prove-it.json must be a JSON object")
        for kind in ALL_CHECKS:
            value = config.get(kind)
            if value is False:
                disabled.add(kind)
                continue
            argv = _to_argv(value)
            if argv:
                commands[kind] = argv
    for detector in (_detect_node, _detect_python, _detect_go, _detect_rust, _detect_make):
        for kind, argv in detector(repo).items():
            if kind not in disabled:
                commands.setdefault(kind, argv)
    return commands, disabled


# --------------------------------------------------------------------------
# Running checks
# --------------------------------------------------------------------------


def _decode(data) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data


def _tail(text: str, count: int) -> str:
    return "\n".join(text.strip().splitlines()[-count:])


def run_check(kind: str, argv: list, repo: Path, timeout: int) -> CheckResult:
    display = shlex.join(argv)
    exe = _find_exe(argv[0])
    if exe is None:
        return CheckResult(kind, "NOT_RUN", display, note=f"command not found: {argv[0]}")
    if exe == sys.executable and shutil.which(argv[0]) != exe:
        display = shlex.join([exe] + argv[1:])  # report the Python actually used
    start = time.monotonic()
    try:
        proc = subprocess.run(
            [exe] + argv[1:],
            cwd=str(repo),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return CheckResult(
            kind,
            "TIMEOUT",
            display,
            None,
            round(time.monotonic() - start, 1),
            _tail(_decode(exc.stdout), TAIL_LINES),
            f"timed out after {timeout}s",
        )
    except OSError as exc:
        return CheckResult(kind, "NOT_RUN", display, note=f"could not start: {exc}")

    seconds = round(time.monotonic() - start, 1)
    output = _decode(proc.stdout)
    note = ""
    if proc.returncode == 0:
        status = "PASS"
    elif "No module named pytest" in output:
        status = "NOT_RUN"
        note = "pytest is not installed for this Python"
    else:
        status = "FAIL"
        if kind == "test" and proc.returncode == 5 and "pytest" in display:
            note = "pytest collected no tests"
    tail_count = PASS_TAIL_LINES if status == "PASS" else TAIL_LINES
    return CheckResult(
        kind,
        status,
        display,
        None if status == "NOT_RUN" else proc.returncode,
        seconds,
        _tail(output, tail_count),
        note,
    )


# --------------------------------------------------------------------------
# Git diff
# --------------------------------------------------------------------------


def _norm(path: str) -> str:
    path = path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _git(repo: Path, *args: str, input_text: str | None = None):
    return subprocess.run(
        ["git", "-c", "core.quotepath=off", *args],
        cwd=str(repo),
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )


def collect_diff(repo: Path, base: str, ignore: frozenset = frozenset()) -> DiffInfo:
    info = _collect_diff(repo, base)
    for path in ignore:
        info.files.pop(path, None)
        info.added.pop(path, None)
    return info


def _resolve_ref(repo: Path, base: str) -> tuple:
    """Return (ref, note). ref is None when no diff is available."""
    try:
        inside = _git(repo, "rev-parse", "--is-inside-work-tree")
    except OSError:
        return None, "git is not installed"
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return None, "not a git repository"
    resolved = _git(repo, "rev-parse", "--verify", "--quiet", base + "^{commit}")
    if resolved.returncode == 0:
        return base, ""
    if base != "HEAD":
        return None, f"base '{base}' is not a valid commit"
    # A repo with no commits yet: compare against the empty tree.
    empty = _git(repo, "hash-object", "-t", "tree", "--stdin", input_text="")
    ref = empty.stdout.strip()
    if empty.returncode != 0 or not ref:
        return None, "could not resolve an empty tree"
    return ref, ""


def rev_sha(repo: Path, ref: str):
    """Return the commit sha for ref, or None (no git, no commits, bad ref)."""
    try:
        result = _git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
    except OSError:
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def is_ancestor(repo: Path, older: str, newer: str) -> bool:
    """True if commit older is newer or one of its ancestors."""
    try:
        return _git(repo, "merge-base", "--is-ancestor", older, newer).returncode == 0
    except OSError:
        return False


def working_changes(repo: Path, base: str, ignore: frozenset = frozenset()):
    """Return (changed paths, fingerprint) against base, or None without git.

    The fingerprint is a hash of the full patch plus untracked file contents,
    so any edit after a run gives a different value. The Stop hook uses it to
    tell whether the last logged run still matches the files on disk.
    """
    try:
        ref, _ = _resolve_ref(repo, base)
    except OSError:
        return None
    if ref is None:
        return None
    names = _git(repo, "diff", "--relative", "--name-only", "--no-renames", ref)
    tracked = sorted(p for p in (_norm(n) for n in names.stdout.splitlines() if n) if p not in ignore)
    digest = hashlib.sha256()
    if tracked:
        patch = subprocess.run(
            ["git", "diff", "--relative", "--binary", "--no-renames", "--no-color", ref, "--"]
            + [":(literal)" + path for path in tracked],
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        digest.update(patch.stdout)
    others = _git(repo, "ls-files", "--others", "--exclude-standard")
    untracked = sorted(
        p for p in (_norm(n) for n in others.stdout.splitlines() if n)
        if not p.startswith(LOG_DIR + "/") and p not in ignore
    )
    for path in untracked:
        digest.update(b"\0untracked\0" + path.encode("utf-8") + b"\0")
        try:
            stat = (repo / path).stat()
            if stat.st_size > MAX_UNTRACKED_BYTES:
                # Big files: size and mtime instead of reading every byte.
                digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
            else:
                digest.update((repo / path).read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
    return tracked + untracked, digest.hexdigest()


def _collect_diff(repo: Path, base: str) -> DiffInfo:
    ref, note = _resolve_ref(repo, base)
    if ref is None:
        return DiffInfo(False, base, note=note)

    info = DiffInfo(True, base)
    names = _git(repo, "diff", "--relative", "--name-status", "--no-renames", ref)
    for line in names.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            info.files[_norm(parts[-1])] = parts[0][:1]

    patch = _git(repo, "diff", "--relative", "-U0", "--no-renames", "--no-color", ref)
    current = None
    in_hunk = False
    for line in patch.stdout.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
            current = None
        elif line.startswith("@@"):
            in_hunk = True
        elif not in_hunk and line.startswith("+++ "):
            target = line[4:].split("\t")[0]
            current = _norm(target[2:]) if target.startswith("b/") else None
            if current:
                info.added.setdefault(current, [])
        elif in_hunk and line.startswith("+") and current:
            info.added[current].append(line[1:])

    others = _git(repo, "ls-files", "--others", "--exclude-standard")
    for rel in others.stdout.splitlines():
        path = _norm(rel)
        if path.startswith(LOG_DIR + "/"):
            continue
        info.files[path] = "?"
        candidate = repo / rel
        try:
            if candidate.is_file() and candidate.stat().st_size <= MAX_UNTRACKED_BYTES:
                info.added[path] = candidate.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
        except OSError:
            pass
    return info


# --------------------------------------------------------------------------
# Claims
# --------------------------------------------------------------------------


def load_claims(source: str) -> list:
    try:
        raw = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"cannot read claims: {exc}")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise UsageError(f"claims are not valid JSON: {exc}")
    if isinstance(data, dict):
        data = data.get("claims")
    if not isinstance(data, list):
        raise UsageError('claims must be a JSON list or {"claims": [...]}')
    claims = []
    for index, item in enumerate(data, 1):
        if not isinstance(item, dict) or not isinstance(item.get("claim"), str) or not item["claim"].strip():
            raise UsageError(f"claim #{index} needs a non-empty string field 'claim'")
        for key in ("files", "symbols"):
            value = item.get(key, [])
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise UsageError(f"claim #{index}: '{key}' must be a list of strings")
        if not isinstance(item.get("kind", ""), str):
            raise UsageError(f"claim #{index}: 'kind' must be a string")
        if "command" in item and _to_argv(item["command"]) is None:
            raise UsageError(f"claim #{index}: 'command' must be a non-empty string or list of strings")
        claims.append(item)
    return claims


def _symbol_in(symbol: str, lines: list) -> bool:
    pattern = re.compile(r"(?<!\w)" + re.escape(symbol) + r"(?!\w)")
    return any(pattern.search(line) for line in lines)


def _command_part(result: CheckResult, label: str) -> list:
    if result.status in ("NOT_CONFIGURED", "NOT_RUN"):
        return ["UNVERIFIED", f"{label} did not run: {result.note or result.status}"]
    if result.status == "PASS":
        return ["SUPPORTED", f"{result.command} exited 0"]
    detail = f"exit {result.exit_code}" if result.exit_code is not None else result.status.lower()
    return ["NOT_SUPPORTED", f"{result.command} did not pass ({detail})"]


def evaluate_claim(claim: dict, checks: dict, diff: DiffInfo,
                   command_result: CheckResult | None = None) -> ClaimResult:
    parts: list = []
    kind = claim.get("kind") or ""
    files = [_norm(f) for f in claim.get("files", [])]

    if command_result is not None:
        parts.append(_command_part(command_result, "claim command"))

    if kind in CHECK_KINDS:
        check_kind = CHECK_KINDS[kind]
        result = checks.get(check_kind)
        if result is None:
            parts.append(["UNVERIFIED", f"{check_kind} check was not run (excluded by --checks)"])
        else:
            parts.append(_command_part(result, f"{check_kind} check"))
    elif kind:
        known = ", ".join(CHECK_KINDS)
        parts.append(["UNVERIFIED", f"unknown kind '{kind}' was not checked (use one of: {known})"])

    for path in files:
        if not diff.available:
            parts.append(["UNVERIFIED", f"{path}: no diff available ({diff.note})"])
        elif path in diff.files:
            parts.append(["SUPPORTED", f"{path} changed ({STATUS_NAMES.get(diff.files[path], diff.files[path])})"])
        else:
            parts.append(["NOT_SUPPORTED", f"{path} is not changed against {diff.base}"])

    scope = files or list(diff.added)
    for symbol in claim.get("symbols", []):
        if not diff.available:
            parts.append(["UNVERIFIED", f"'{symbol}': no diff available ({diff.note})"])
            continue
        hit = next((f for f in scope if _symbol_in(symbol, diff.added.get(f, []))), None)
        if hit:
            parts.append(["SUPPORTED", f"'{symbol}' appears in lines added to {hit}"])
        else:
            where = "the claimed files" if files else "the diff"
            parts.append(["NOT_SUPPORTED", f"'{symbol}' is not in any line added in {where}"])

    if not parts:
        parts.append(["UNVERIFIED", "nothing checkable: add 'files', 'symbols', 'command' or a 'kind' such as tests_pass"])

    verdicts = {p[0] for p in parts}
    if "NOT_SUPPORTED" in verdicts:
        verdict = "NOT_SUPPORTED"
    elif "UNVERIFIED" in verdicts:
        verdict = "UNVERIFIED"
    else:
        verdict = "SUPPORTED"
    tail = ""
    if command_result is not None and command_result.status != "PASS":
        tail = command_result.output_tail
    return ClaimResult(claim["claim"].strip(), kind, verdict, parts, tail)


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def summarize(checks: list, claim_results: list, diff: DiffInfo, claims: list, strict: bool):
    gaps: list = []
    for check in checks:
        if check.status in ("NOT_RUN", "NOT_CONFIGURED"):
            gaps.append(f"{check.kind} check not run: {check.note or check.status}")
    for result in claim_results:
        reasons = [text for verdict, text in result.details if verdict == "UNVERIFIED"]
        if result.verdict == "UNVERIFIED":
            gaps.append(f"claim not verified: {result.claim} ({'; '.join(reasons)})")

    unclaimed: list = []
    if claims and diff.available:
        claimed = {_norm(f) for c in claims for f in c.get("files", [])}
        unclaimed = sorted(path for path in diff.files if path not in claimed)

    failures: list = []
    for check in checks:
        if check.status in ("FAIL", "TIMEOUT"):
            failures.append(f"{check.kind} check {check.status.lower()}")
    for result in claim_results:
        if result.verdict == "NOT_SUPPORTED":
            failures.append(f"claim not supported: {result.claim}")
    if strict:
        failures.extend(f"strict: {gap}" for gap in gaps)

    state = "FAILED" if failures else ("PARTIAL" if gaps else "VERIFIED")
    return state, failures, gaps, unclaimed


def render_report(repo: Path, diff: DiffInfo, checks: list, claim_results: list,
                  state: str, failures: list, gaps: list, unclaimed: list) -> str:
    out = ["PROVE IT REPORT", f"repo: {repo}"]
    if diff.available:
        out.append(f"diff: {len(diff.files)} file(s) changed vs {diff.base}")
    else:
        out.append(f"diff: unavailable ({diff.note})")

    out += ["", "CHECKS"]
    if not checks:
        out.append("  (none requested)")
    for check in checks:
        head = f"[{check.status}] {check.kind}"
        if check.command:
            head += f"  {check.command}"
        if check.exit_code is not None:
            head += f"  exit {check.exit_code}"
        if check.seconds:
            head += f"  {check.seconds}s"
        out.append(head)
        if check.note:
            out.append(f"    note: {check.note}")
        out.extend(f"    | {line}" for line in check.output_tail.splitlines())

    out += ["", "CLAIMS"]
    if not claim_results:
        out.append("  (no claims provided)")
    for result in claim_results:
        out.append(f"[{result.verdict.replace('_', ' ')}] {result.claim}")
        for verdict, text in result.details:
            out.append(f"    - {verdict.replace('_', ' ').lower()}: {text}")
        out.extend(f"    | {line}" for line in result.output_tail.splitlines())

    out += ["", "NOT VERIFIED"]
    for gap in gaps:
        out.append(f"  - {gap}")
    for path in unclaimed[:MAX_UNCLAIMED_SHOWN]:
        out.append(f"  - changed but not claimed: {path}")
    if len(unclaimed) > MAX_UNCLAIMED_SHOWN:
        out.append(f"  - ... and {len(unclaimed) - MAX_UNCLAIMED_SHOWN} more changed files not claimed")
    out.append("  - Passing checks show only what those checks cover, not that the change is correct.")

    out += ["", f"RESULT: {state}"]
    out.extend(f"  ! {failure}" for failure in failures)
    return "\n".join(out)


# --------------------------------------------------------------------------
# Log and history
# --------------------------------------------------------------------------


def append_log(repo: Path, state: str, base: str, checks: list, claim_results: list,
               **extra) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "state": state,
        "base": base,
        **extra,
        "checks": [{"kind": c.kind, "status": c.status, "exit_code": c.exit_code} for c in checks],
        "claims": [{"claim": r.claim, "kind": r.kind, "verdict": r.verdict} for r in claim_results],
    }
    directory = repo / LOG_DIR
    try:
        directory.mkdir(parents=True, exist_ok=True)
        ignore = directory / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
        with (directory / LOG_FILE).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError as exc:
        print(f"prove-it: could not write log: {exc}", file=sys.stderr)


def print_history(repo: Path) -> int:
    path = repo / LOG_DIR / LOG_FILE
    if not path.is_file():
        print("No prove-it history yet. Run a verification first.")
        return 0
    runs = []
    for line in _read_text(path).splitlines():
        try:
            runs.append(json.loads(line))
        except ValueError:
            continue
    states: dict = {}
    verdicts: dict = {}
    by_kind: dict = {}
    for run in runs:
        states[run.get("state", "?")] = states.get(run.get("state", "?"), 0) + 1
        for claim in run.get("claims", []):
            verdict = claim.get("verdict", "?")
            kind = claim.get("kind") or "other"
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
            by_kind.setdefault(kind, {})
            by_kind[kind][verdict] = by_kind[kind].get(verdict, 0) + 1
    print(f"PROVE IT HISTORY: {len(runs)} run(s)")
    print("runs by result: " + ", ".join(f"{k}={v}" for k, v in sorted(states.items())))
    print("claims by verdict: " + ", ".join(f"{k}={v}" for k, v in sorted(verdicts.items())))
    for kind, counts in sorted(by_kind.items()):
        print(f"  {kind}: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print("last runs:")
    for run in runs[-5:]:
        print(f"  {run.get('ts', '?')}  {run.get('state', '?')}  ({len(run.get('claims', []))} claims)")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prove_it",
        description="Run real checks and match an agent's claims against the git diff.",
    )
    parser.add_argument("--repo", default=".", help="project folder (default: current directory)")
    parser.add_argument("--claims", help="JSON file with the claims, or '-' to read stdin")
    parser.add_argument("--base", default="HEAD", help="git commit to diff against (default: HEAD)")
    parser.add_argument("--checks", default=",".join(ALL_CHECKS), help="comma list of: test,build,lint")
    parser.add_argument("--timeout", type=int, default=600, help="seconds allowed per check (default: 600)")
    parser.add_argument("--strict", action="store_true", help="treat unverified items as failures")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--no-log", action="store_true", help="do not write .prove-it/log.jsonl")
    parser.add_argument("--history", action="store_true", help="summarize past runs and exit")
    return parser


def _run(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        raise UsageError(f"not a folder: {args.repo}")
    if args.history:
        return print_history(repo)
    if args.base.startswith("-"):
        raise UsageError("--base must be a commit, not an option")
    if args.timeout <= 0:
        raise UsageError("--timeout must be positive")
    selected = [name.strip() for name in args.checks.split(",") if name.strip()]
    unknown = [name for name in selected if name not in ALL_CHECKS]
    if unknown:
        raise UsageError(f"unknown check(s): {', '.join(unknown)} (choose from {', '.join(ALL_CHECKS)})")

    claims = load_claims(args.claims) if args.claims else []
    commands, disabled = detect_commands(repo)
    ignore = set()
    if args.claims and args.claims != "-":
        try:
            ignore.add(_norm(str(Path(args.claims).resolve().relative_to(repo))))
        except ValueError:
            pass  # claims file is outside the repo
    # Diff before running checks, so files the checks create are not counted.
    diff = collect_diff(repo, args.base, frozenset(ignore))

    checks: list = []
    for kind in selected:
        if kind in commands:
            checks.append(run_check(kind, commands[kind], repo, args.timeout))
        elif kind in disabled:
            checks.append(CheckResult(kind, "NOT_CONFIGURED", note="disabled in .prove-it.json"))
        else:
            checks.append(
                CheckResult(kind, "NOT_CONFIGURED", note="no command found; set one in .prove-it.json")
            )

    by_kind = {check.kind: check for check in checks}
    claim_results = []
    for claim in claims:
        command_result = None
        if "command" in claim:
            command_result = run_check("claim", _to_argv(claim["command"]), repo, args.timeout)
        claim_results.append(evaluate_claim(claim, by_kind, diff, command_result))
    state, failures, gaps, unclaimed = summarize(checks, claim_results, diff, claims, args.strict)

    if args.json:
        payload = {
            "state": state,
            "repo": str(repo),
            "base": args.base,
            "diff": {"available": diff.available, "note": diff.note, "files": diff.files},
            "checks": [asdict(check) for check in checks],
            "claims": [asdict(result) for result in claim_results],
            "not_verified": gaps,
            "changed_but_not_claimed": unclaimed,
            "failures": failures,
        }
        print(json.dumps(payload, indent=2))
    else:
        print(render_report(repo, diff, checks, claim_results, state, failures, gaps, unclaimed))

    if not args.no_log:
        # Snapshot after the checks, so files they generate do not make the
        # run look stale to the Stop hook. The snapshot is HEAD plus a
        # fingerprint of everything uncommitted.
        snapshot = working_changes(repo, "HEAD", frozenset(ignore))
        append_log(
            repo, state, args.base, checks, claim_results,
            head=rev_sha(repo, "HEAD"),
            fingerprint=snapshot[1] if snapshot else None,
            base_sha=rev_sha(repo, args.base),
            ignore=sorted(ignore),
            unclaimed=unclaimed,
        )
    return 1 if state == "FAILED" else 0


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except UsageError as exc:
        print(f"prove-it: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
