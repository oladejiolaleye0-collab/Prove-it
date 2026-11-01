# Prove it

**Make your coding agent do exactly what it said, stay on task, and prove it.**

[![tests](https://github.com/oladejiolaleye0-collab/Prove-it/actions/workflows/test.yml/badge.svg)](https://github.com/oladejiolaleye0-collab/Prove-it/actions/workflows/test.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)
![no dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)

Coding agents say "fixed, all tests pass" without running anything. They edit files that have nothing to do with the task, quietly drop the hard part of a request, and make a red test green by weakening it. Prove it is a skill plus a set of Claude Code hooks that stops each of those:

| The agent... | Prove it... |
|---|---|
| says it's done without evidence | blocks the end of the turn until a logged verification run backs the report |
| wanders off task | makes it write a **contract** (task, files in scope, acceptance criteria) before its first edit, and blocks or flags every change outside that scope, including ones made with `sed` or scripts |
| moves the goalposts | locks the criteria once work starts |
| writes a test that would pass anyway | requires `fails_before` tests to **fail on the old code** and pass on the new |
| weakens tests to get green | fails the run on removed assertions, new skip/only markers and deleted tests |
| claims more than it proved | reports exactly what is verified, not verified and failed, and checks the agent says so |

Standard library Python and git. No packages, no services, nothing leaves your machine.

## What it looks like

The agent fixed the bug and proved it, but also touched a file outside its contract. Real output, trimmed to the key lines:

```
CONTRACT
task: double the price
[OUT OF SCOPE] other.py

CRITERIA
[SUPPORTED] c1: price(2) returns 4
    - supported: python -m unittest -q test_shop exited 0
    - supported: fails on the code before the change (62e955ea2a62) (exit 1) and passes now

RESULT: FAILED
  ! changed outside the contract's scope: other.py
```

When it then tries to finish with "Done, everything works.", the Stop hook sends it back:

```
Prove it: the last prove-it run FAILED but your message does not say what failed. Before you say the work is done: ...
```

## Install

You need Python 3.8+ and git.

### Claude Code plugin (recommended)

In Claude Code:

```
/plugin install prove-it --marketplace oladejiolaleye0-collab/Prove-it
```

On older Claude Code versions, add the marketplace first:

```
/plugin marketplace add oladejiolaleye0-collab/Prove-it
/plugin install prove-it@prove-it
```

This installs the skill and all four hooks. Start a new session afterwards. The hooks run `py -3` where it exists (Windows), otherwise `python3`.

### Installer script (any setup, best on Windows)

```bash
git clone https://github.com/oladejiolaleye0-collab/Prove-it.git ~/.claude/skills/prove-it
python3 ~/.claude/skills/prove-it/install.py
```

The clone installs the skill; `install.py` adds the hooks to `~/.claude/settings.json` with absolute paths to your Python, keeping your other settings and writing a backup. Use `--project PATH` for one project, `--uninstall` to remove, `--dry-run` to preview. Use the plugin or the installer, not both.

### Codex and other agents

Clone the repo into your project as `prove-it/` and paste [`AGENTS.snippet.md`](AGENTS.snippet.md) into your `AGENTS.md`. The verifier works the same; without hooks, staying on course relies on the agent.

## Try it in two minutes

```bash
python3 examples/try_it.py ~/prove-it-demo
```

This creates a small git project with a real bug (`apply_discount` subtracts the percent instead of applying it), a test suite that misses it, and an unrelated `notes.md`. It prints a prompt to paste into a new Claude Code session there. Pass `--no-hooks` if you installed the plugin.

Watch the agent write a contract before editing, add a test that fails on the old code, leave `notes.md` alone, and finish with a report that shows its evidence.

## How it works

1. **Contract first.** Before its first edit, the agent writes `.prove-it/contract.json`:

   ```json
   {
     "task": "Fix apply_discount so percent is a percentage",
     "scope": ["cart.py", "test_cart.py"],
     "criteria": [
       {"id": "c1", "text": "10% off 20.00 is 18.00",
        "command": "python3 -m unittest test_cart", "fails_before": true}
     ]
   }
   ```

2. **On course.** The hooks check every edit and shell command against the scope as the work happens. If the task really needs another file, the agent adds an amendment with a reason, and it shows up in the report.
3. **Proof.** `scripts/prove_it.py` runs your test, build and lint commands and each criterion. For `fails_before`, it checks out the old code in a temporary git worktree, copies in the new tests, and confirms they fail there. It also checks every changed file against the scope and every test file for weakening.
4. **Honest report.** The agent can't finish until a run matches the current files and its message owns up to anything failed or unverified.

The details, every setting and every file format are in the [reference](docs/REFERENCE.md).

## What it can't do

- **It checks the criteria the agent writes.** If a requirement isn't in the contract, nothing checks it. Read the contract; it's in every report.
- **A test only proves what it tests.** `fails_before` shows the test depends on the change, not that the change is complete.
- **The weakening check counts lines,** so swapping a strong assertion for a weak one isn't caught.
- **Enforcement is Claude Code only.** Other agents get the verifier without the hooks.
- **It runs your project's own commands.** Use it in repositories you trust.

More in [Limits](docs/REFERENCE.md#limits-you-should-know-about).

## Status

v2.0.0. Tests run on Linux, macOS and Windows with Python 3.8 to 3.13. Live Claude Code sessions confirmed the Stop hook, the contract reminder, `fails_before` and honest reporting; the after-command scope check is covered by tests and a scripted replay. See the [changelog](CHANGELOG.md).

Feedback from real use is the most useful thing right now: [open an issue](https://github.com/oladejiolaleye0-collab/Prove-it/issues) with what the agent did and what Prove it said. Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
