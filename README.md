# Prove it

**Prove it** is an agent skill that blocks unverified "done" claims: it runs your tests and checks, compares each claim against the actual git diff and command output, and reports what was verified and what wasn't.

Coding agents often say "fixed, all tests pass" without running anything, or claim they changed a file they never touched. Prove it gives the agent a script that produces real evidence, and a protocol that makes the agent report only what that evidence supports.

## What it does

1. The agent lists the claims it is about to make in `.prove-it/claims.json`.
2. `scripts/prove_it.py` auto-detects and runs your project's test, build and lint commands.
3. Each claim is checked: files against the git diff, symbols against the lines added in the diff, and `tests_pass` / `build_ok` / `lint_ok` against the real exit codes.
4. The report ends with an explicit **NOT VERIFIED** list, including files that changed but that no claim mentioned.
5. Every run is appended to a local log, and `--history` shows how often claims held up.

Example (real output from a demo repo):

```
PROVE IT REPORT
diff: 2 file(s) changed vs HEAD

CHECKS
[PASS] test  python3 -m unittest -q  exit 0  0.1s
    | Ran 1 test in 0.000s
    | OK

CLAIMS
[SUPPORTED] Added apply_discount to the shop module
    - supported: shop.py changed (modified)
    - supported: 'apply_discount' appears in lines added to shop.py
[NOT SUPPORTED] Updated the docs
    - not supported: docs.md is not changed against HEAD
[SUPPORTED] All tests pass
    - supported: python3 -m unittest -q exited 0
[UNVERIFIED] Handles negative prices correctly
    - unverified: nothing checkable: add 'files', 'symbols' or a 'kind' such as tests_pass

NOT VERIFIED
  - claim not verified: Handles negative prices correctly (nothing checkable: ...)
  - changed but not claimed: notes.txt
  - Passing checks show only what those checks cover, not that the change is correct.

RESULT: FAILED
  ! claim not supported: Updated the docs
```

## Install

Requires Python 3.8+ and git. No other dependencies.

On Windows, type `python` wherever this README says `python3`. Checks in `.prove-it.json` that start with `python` or `python3` run with the same Python that runs Prove it when the command on PATH is missing or is the Microsoft Store placeholder, so one config works on every OS. The report shows which Python was used.

**Claude Code**

```bash
git clone https://github.com/oladejiolaleye0-collab/Prove-it.git ~/.claude/skills/prove-it
```

Use `<your-project>/.claude/skills/prove-it` instead to install it for one project.

To make Claude Code actually stop and verify, also add the Stop hook below.

**Claude Code Stop hook (hard enforcement)**

The skill alone relies on the agent choosing to follow it. The Stop hook blocks Claude from ending its turn with a "done" claim while there are uncommitted changes that no prove-it run covers. Add this to `~/.claude/settings.json` (or `.claude/settings.json` in a project):

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {"type": "command", "command": "python3 ~/.claude/skills/prove-it/scripts/stop_hook.py"}
        ]
      }
    ]
  }
}
```

On Windows, use `python` instead of `python3` and the full path to `stop_hook.py`.

When Claude tries to stop, the hook lets it through if any of these hold:

- there are no uncommitted changes (or the folder is not a git repo)
- the last message does not sound like a done claim (words such as done, fixed, works, passing)
- the latest logged prove-it run matches the files on disk exactly, and if that run FAILED, the message says what failed

Otherwise it blocks, and Claude is told to run prove-it and report the result. Any edit after a run makes that run stale, because each logged run stores a fingerprint of the diff.

To stop a stuck loop, the hook blocks at most `max_blocks` times in a row (default 2). After that it lets Claude stop and tells you the work is unverified. Settings go in `.prove-it.json`:

```json
{"hook": {"mode": "claims", "max_blocks": 2}}
```

| `mode` | Effect |
|---|---|
| `claims` | Check only when the last message sounds like a done claim (default) |
| `always` | Check every stop while there are uncommitted changes |
| `off` | Disable the hook for this project |

**Codex and other agents that read `AGENTS.md`**

```bash
git clone https://github.com/oladejiolaleye0-collab/Prove-it.git prove-it
```

Then paste the contents of `AGENTS.snippet.md` into your project's `AGENTS.md`, adjusting the path if you cloned elsewhere.

**Without an agent**

```bash
python3 scripts/prove_it.py --claims claims.json
```

## Claims file

```json
[
  {"claim": "Fixed the login redirect bug", "files": ["src/auth.py"], "symbols": ["redirect_after_login"]},
  {"claim": "All tests pass", "kind": "tests_pass"}
]
```

| Field | Meaning | How it is checked |
|---|---|---|
| `claim` | What the agent says (required) | Shown in the report |
| `files` | Files the agent says it changed | Present in the git diff, including new untracked files |
| `symbols` | Names the agent says it added or changed | Whole-word match in lines added by the diff |
| `kind` | `tests_pass`, `build_ok` or `lint_ok` | The matching command exited 0. Any other value is reported as unverified |

A claim is **SUPPORTED** only if every part is supported, **NOT SUPPORTED** if any part is contradicted, and **UNVERIFIED** otherwise.

## Options

| Option | Effect |
|---|---|
| `--claims FILE` | Claims to check (`-` reads stdin) |
| `--base REF` | Diff against a commit, for example `main`, when work is already committed (default `HEAD`) |
| `--checks test,build,lint` | Which checks to run |
| `--timeout N` | Seconds allowed per check (default 600) |
| `--strict` | Treat unverified items as failures |
| `--json` | Machine-readable report |
| `--no-log` | Do not write `.prove-it/log.jsonl` |
| `--history` | Summarize past runs |

Exit code: `0` verified or partial, `1` failed (a check failed or a claim is contradicted, or anything unverified under `--strict`), `2` bad input.

## Which commands it runs

Detected automatically, first match wins per check: `.prove-it.json`, `package.json` scripts (npm, pnpm, yarn or bun), Python (pytest, ruff), Go, Rust, then `Makefile` targets named `test`, `build` or `lint`.

Override or disable in `.prove-it.json` at the project root:

```json
{
  "test": "python3 -m unittest discover -s tests",
  "build": ["make", "build"],
  "lint": false
}
```

A command is a string or a list of arguments. `false` disables that check.

## Limits you should know about

- **It checks structure, not correctness.** A claim passes if the file changed and the symbol was added. It cannot tell whether the change is right. Passing tests only prove what those tests cover.
- **Without the Stop hook, the agent can skip it.** The skill alone relies on the agent following the protocol. The hook enforces it in Claude Code only. It also checks only uncommitted changes, so work the agent has already committed is not covered. Claim words are matched with a simple word list, so it can miss a claim or flag a sentence that is not one. Other agents still rely on `AGENTS.md`.
- **It runs your project's own commands.** That is `npm run test`, `make test` and so on, plus anything in `.prove-it.json`. Only run it in repositories you trust.
- **Some "passing" runs prove little.** For example `go test` exits 0 when a package has no test files. Prove it reports the exit code; read the output lines it shows.
- **Output can contain secrets.** The report prints the tail of check output. The log stores only statuses and claim text, and `.prove-it/` ignores itself in git.
- **Changes to `.prove-it.json` show up in the diff on purpose.** If an agent edits it to dodge a check, that appears under "changed but not claimed" unless the agent claims it.

## Status

v1. The script and tests run and pass (`python3 -m unittest discover -s tests`). It has not yet been tested inside long real sessions of Claude Code or Codex, so expect rough edges in the protocol wording. Issues and pull requests are welcome.

The Stop hook is new and covered by unit tests, but has not yet been used in real sessions.

Planned: deeper claim checks (callers, deletions), more ecosystems, and optional long-term history in an external memory tool.

## Development

```bash
python3 -m unittest discover -s tests
```

## License

MIT
