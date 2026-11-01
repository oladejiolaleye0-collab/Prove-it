# Changelog

## 2.0.0

Contracts and on-course enforcement.

- **Contracts**: `.prove-it/contract.json` with task, scope, acceptance criteria, amendments and declared test changes. The verifier checks every changed file against the scope and runs every criterion.
- **`fails_before`**: a criterion's command must fail on the code before the change (checked in a temporary git worktree with the new tests copied in) and pass after it. Tests that pass either way are rejected.
- **Test integrity**: removed assertions, new skip/only markers and deleted test files fail the run unless the contract allows them with a reason.
- **Hooks**: PreToolUse blocks edits before a contract, outside scope, and to Prove it's own files, and stops criteria being dropped mid-turn. PostToolUse checks the files that actually changed after every edit and shell command. UserPromptSubmit reminds the agent of the protocol. Stop requires a run of the current contract.
- **Report**: CONTRACT and CRITERIA sections, and "in scope but unchanged".
- **Packaging**: Claude Code plugin and marketplace, `install.py`, and a two-minute demo (`examples/try_it.py`).

## 1.1.0

- Stop hook: blocks "done" until a logged run covers the current files, commits made this turn, and every changed file; requires FAILED and PARTIAL results to be reported.
- Per-claim `command`.
- `--repo` works on a subfolder of a git repository.
- Windows fixes: `python3` placeholder, quoted commands, unknown claim kinds reported.
- CI on Linux, macOS and Windows with Python 3.8 to 3.13.

## 1.0.0

- First release: claims checked against the git diff, test/build/lint commands detected and run, NOT VERIFIED list, run log and `--history`.
