# Prove it

[![tests](https://github.com/oladejiolaleye0-collab/Prove-it/actions/workflows/test.yml/badge.svg)](https://github.com/oladejiolaleye0-collab/Prove-it/actions/workflows/test.yml)

**Prove it** is an agent skill that makes a coding agent do exactly what it said it would, stay on task while doing it, and prove the result with real evidence.

Coding agents often say "fixed, all tests pass" without running anything, edit files that have nothing to do with the task, quietly drop the hard part of a request, or "fix" a failing test by weakening it. Prove it closes each of those gaps:

| Failure | What Prove it does |
|---|---|
| Says it is done without evidence | Blocks the end of the turn until a logged verification run backs the report |
| Wanders off task | The agent commits to a **contract** (task, files in scope, acceptance criteria) before editing; edits outside the scope are blocked |
| Moves the goalposts | Criteria cannot be dropped or weakened once editing starts |
| Writes a test that would pass anyway | `fails_before` criteria must **fail on the code before the change** and pass after it |
| Weakens tests to get green | Removed assertions, new skip/only markers and deleted test files fail the run |
| Reports more than it proved | The report says exactly what is verified, not verified and failed, and the hook checks the agent says so |

## How it works

1. **Contract first.** Before its first edit, the agent writes `.prove-it/contract.json`:

   ```json
   {
     "task": "Reject negative prices in price()",
     "scope": ["shop.py", "tests/test_shop.py"],
     "criteria": [
       {"id": "c1", "text": "price(-1) raises ValueError",
        "command": "python3 -m pytest tests/test_shop.py -k negative", "fails_before": true},
       {"id": "c2", "text": "Existing prices are unchanged",
        "command": "python3 -m pytest tests/test_shop.py"}
     ]
   }
   ```

2. **On course.** With the Claude Code hook installed, every edit is checked against the scope as it happens. If the agent really needs another file, it adds an amendment with a reason, which shows up in the report.
3. **Proof.** `scripts/prove_it.py` runs the project's test, build and lint commands and each criterion's command. For `fails_before` criteria it checks out the code from before the change in a temporary git worktree, copies in the new tests, and confirms they fail there. It also checks every changed file against the scope and every test file for weakening.
4. **Honest report.** The report ends with **NOT VERIFIED** and **RESULT**. The hook will not let the agent finish until a run matches the current files and the agent's message admits any failures or gaps.

Example report (real output, with the repo line dropped and the Python path shortened; the agent fixed `price()` but also changed `other.py` through the shell):

```
PROVE IT REPORT
diff: 3 file(s) changed vs HEAD

CONTRACT
task: double the price
[OUT OF SCOPE] other.py

CRITERIA
[SUPPORTED] c1: price(2) returns 4
    - supported: python.exe -m unittest -q test_shop exited 0
    - supported: fails on the code before the change (62e955ea2a62) (exit 1) and passes now

CHECKS
[PASS] test  python.exe -m unittest -q  exit 0  0.2s
    | Ran 1 test in 0.000s
    |
    | OK
[NOT_CONFIGURED] build
    note: disabled in .prove-it.json
[NOT_CONFIGURED] lint
    note: disabled in .prove-it.json

CLAIMS
  (no claims provided)

NOT VERIFIED
  - build check not run: disabled in .prove-it.json
  - lint check not run: disabled in .prove-it.json
  - Passing checks show only what those checks cover, not that the change is correct.

RESULT: FAILED
  ! changed outside the contract's scope: other.py
```

## Install

Requires Python 3.8+ and git. No other dependencies.

On Windows, type `python` wherever this README says `python3`. Checks and criteria that start with `python` or `python3` run with the same Python that runs Prove it when the command on PATH is missing or is the Microsoft Store placeholder, so one config works on every OS.

**Claude Code: the skill**

```bash
git clone https://github.com/oladejiolaleye0-collab/Prove-it.git ~/.claude/skills/prove-it
```

Use `<your-project>/.claude/skills/prove-it` instead to install it for one project.

**Claude Code: the hook (enforcement)**

The skill alone relies on the agent choosing to follow it. The hook enforces it. Register the same script for four events in `~/.claude/settings.json` (or `.claude/settings.json` in a project):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {"hooks": [{"type": "command", "command": "python3 ~/.claude/skills/prove-it/scripts/stop_hook.py"}]}
    ],
    "PreToolUse": [
      {"matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash",
       "hooks": [{"type": "command", "command": "python3 ~/.claude/skills/prove-it/scripts/stop_hook.py"}]}
    ],
    "PostToolUse": [
      {"matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash",
       "hooks": [{"type": "command", "command": "python3 ~/.claude/skills/prove-it/scripts/stop_hook.py"}]}
    ],
    "Stop": [
      {"hooks": [{"type": "command", "command": "python3 ~/.claude/skills/prove-it/scripts/stop_hook.py"}]}
    ]
  }
}
```

On Windows, use `python` and the full path to `stop_hook.py`. What each event does:

- **UserPromptSubmit**: records the commit and the files already changed at the start of the turn, unlocks the contract for the new turn, and reminds the agent of the protocol (so it works even where the skill is not installed).
- **PreToolUse**: denies, with a reason the agent can act on:
  - edits before a contract exists (when `contract` is `required`)
  - edits outside the contract's scope and amendments
  - rewriting the contract to drop criteria, drop `fails_before`, or remove amendments after editing started this turn
  - invalid contracts
  - edits to `.prove-it.json`, `.prove-it/log.jsonl`, `.prove-it/hook_state.json` and `.claude/settings*.json`, including through shell commands
- **Stop**: blocks the end of the turn unless the latest logged run matches the current commit, files and contract; covers commits made this turn (`--base`, which the hook names); and the message admits any FAILED or PARTIAL result. Work done under a contract is always checked; otherwise only messages that read like a report of finished work are.

- **PostToolUse**: after every edit and shell command, looks at the files that actually changed this turn, whatever changed them (`sed -i`, redirects, scripts). Changes made before a contract exists, changes outside the scope, and a contract weakened after work started are reported straight back to the agent to fix. The contract is locked as soon as the first change lands. Files that were already changed when the turn started are not blamed on the agent.

PreToolUse can stop an Edit or Write before it happens; for shell commands, PostToolUse catches the change right after, and the verifier checks every changed file against the scope again at the end.

To stop a stuck loop, the Stop hook blocks at most `max_blocks` times in a row (default 2), then lets Claude stop and tells you the work is unverified. Settings go in `.prove-it.json`:

```json
{"hook": {"mode": "claims", "contract": "required", "max_blocks": 2}}
```

| Setting | Values |
|---|---|
| `mode` | `claims` (default): check stops whose message reads like a done report, and all stops after contract work. `always`: check every stop with changes. `off`: disable the hook |
| `contract` | `required` (default): no edits before a contract. `optional`: enforce scope only when a contract exists. `off`: ignore contracts |
| `max_blocks` | Blocks in a row before giving up (default 2) |

**Codex and other agents that read `AGENTS.md`**

```bash
git clone https://github.com/oladejiolaleye0-collab/Prove-it.git prove-it
```

Then paste the contents of `AGENTS.snippet.md` into your project's `AGENTS.md`. The verifier works the same; there is no hook, so staying on course relies on the agent.

**Without an agent**

```bash
python3 scripts/prove_it.py
```

## Contract file

`.prove-it/contract.json`, read automatically (or pass `--contract FILE`).

| Field | Meaning |
|---|---|
| `task` | The request, in one sentence (required) |
| `scope` | Files the agent may change: paths or globs (`*` within a folder, `**` across folders, `dir/` for everything under it) (required) |
| `criteria` | Acceptance criteria (required, at least one): `id`, `text`, and optionally `command`, `fails_before`, `tests` |
| `amendments` | `[{"scope": [...], "reason": "..."}]` scope added during the work, with why |
| `allow_test_changes` | `[{"file": "...", "reason": "..."}]` test weakening the task genuinely requires |

How each criterion is judged:

| Criterion | Verdict |
|---|---|
| `command` exits 0 | SUPPORTED |
| `command` fails | NOT SUPPORTED, with its output |
| no `command` | UNVERIFIED |
| `fails_before` and the command fails on the code before the change | SUPPORTED |
| `fails_before` and the command also passes before the change | NOT SUPPORTED: the test does not show the change made a difference |
| `fails_before` and it fails before for an environment reason (command or module not found, no tests collected) | UNVERIFIED |

For `fails_before`, changed and new test files are copied into the old code automatically; list others in `tests`. Folders such as `node_modules` and `.venv` are linked in, not copied (`"before_links"` in `.prove-it.json` to change the list), and links are removed before the temporary worktree is deleted.

Test files are recognized by common patterns (`test_*.py`, `*_test.go`, `*.test.ts`, `*.spec.js`, `tests/`, `__tests__/` and so on). Override with `"test_patterns"` in `.prove-it.json`. The weakening check runs with or without a contract.

## Claims file

Claims are still supported for anything you want to state beyond the contract, and are required to cover every changed file when there is no contract.

```json
[
  {"claim": "Fixed the login redirect bug", "files": ["src/auth.py"], "symbols": ["redirect_after_login"]},
  {"claim": "Negative prices are rejected", "command": "python3 -m pytest tests/test_shop.py -k negative"},
  {"claim": "All tests pass", "kind": "tests_pass"}
]
```

| Field | Meaning | How it is checked |
|---|---|---|
| `claim` | What the agent says (required) | Shown in the report |
| `files` | Files the agent says it changed | Present in the git diff, including new untracked files |
| `symbols` | Names the agent says it added or changed | Whole-word match in lines added by the diff |
| `kind` | `tests_pass`, `build_ok` or `lint_ok` | The matching command exited 0. Any other value is reported as unverified |
| `command` | A command that demonstrates this claim | It exits 0. Its output is shown if it fails |

## Options

| Option | Effect |
|---|---|
| `--contract FILE` | Contract to check (default `.prove-it/contract.json` if it exists) |
| `--claims FILE` | Claims to check (`-` reads stdin) |
| `--base REF` | Diff against a commit, for example `main`, when work is already committed (default `HEAD`). Also the "before" code for `fails_before` |
| `--checks test,build,lint` | Which checks to run |
| `--timeout N` | Seconds allowed per command (default 600) |
| `--strict` | Treat unverified items as failures |
| `--json` | Machine-readable report |
| `--no-log` | Do not write `.prove-it/log.jsonl` (the hook ignores such runs) |
| `--history` | Summarize past runs |
| `--repo DIR` | Project folder; can be a subfolder of a git repository, such as one package in a monorepo |

Exit code: `0` verified or partial, `1` failed, `2` bad input (including an invalid contract).

## Which commands it runs

Detected automatically, first match wins per check: `.prove-it.json`, `package.json` scripts (npm, pnpm, yarn or bun), Python (pytest, ruff), Go, Rust, then `Makefile` targets named `test`, `build` or `lint`. Override or disable in `.prove-it.json`:

```json
{
  "test": "python3 -m unittest discover -s tests",
  "build": ["make", "build"],
  "lint": false
}
```

## Limits you should know about

- **Correctness is only as good as the criteria.** Prove it shows that each criterion's command passes now and, with `fails_before`, that it depends on the change. It cannot tell whether the criteria capture everything the user wanted. Read the contract: if a requirement is missing there, nothing checks it.
- **The agent writes its own contract.** The hook makes it commit before editing and stops it backing out, but a vague task or a too-wide scope (`**`) weakens everything after. The task text and scope are in every report so you can see them.
- **`fails_before` needs the old code to run.** Generated files, build steps or editable installs that point at the working copy can make the old code behave like the new. Such cases are reported as passing before (NOT SUPPORTED) or as an environment failure (UNVERIFIED), never as proof.
- **The weakening check counts lines.** It looks for assertion-like and skip-like lines, not their meaning. Replacing a strong assertion with a weak one is not detected.
- **Enforcement is Claude Code only.** Other agents get the same verifier through `AGENTS.md`, without the edit guard.
- **It runs your project's own commands,** plus anything in `.prove-it.json` and the contract. Only use it in repositories you trust.
- **Output can contain secrets.** The report prints the tail of command output. The log stores statuses, ids and claim text, and `.prove-it/` ignores itself in git.

## Status

v2. Tests run on Linux, macOS and Windows with Python 3.8 to 3.13 (see the badge above).

Live Claude Code sessions (desktop app, Windows) confirmed the Stop hook, the turn-start reminder (the agent wrote a contract before editing), `fails_before` and an honest PARTIAL report. That session also showed the agent editing through `sed`, which bypassed the Edit-only guard; the PostToolUse check was added for that and is covered by tests, but not yet by a live session. Expect rough edges in the protocol wording. It has not been tried with Codex. Issues and pull requests are welcome.

## Development

```bash
python3 -m unittest discover -s tests
```

## License

MIT
