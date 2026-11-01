# Prove it reference

Everything the [README](../README.md) leaves out: the hooks, every file format, every option, and the limits.

## Hooks

The plugin and `install.py` set these up for you. To configure them by hand, register the same script for four events in `~/.claude/settings.json` (or `.claude/settings.json` in a project):

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

On Windows, use the full path to a real `python.exe` and to `stop_hook.py` (this is what `install.py` writes). What each event does:

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
| `--version` | Print the version |
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
