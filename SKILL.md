---
name: prove-it
description: Use at the start of any coding task that edits files, and before telling the user that work is done, fixed, working, or passing. You commit to a contract (task, files in scope, acceptance criteria with test commands) before editing, stay inside it, then prove each criterion with real command runs, including tests that fail without your change, and report only what the evidence supports.
---

# Prove it

You do exactly what you said you would, nothing outside it, and you prove it.
Unverified "done" claims and quiet drift away from the task are the failures this skill exists to stop.

## 1. Before editing: write the contract

Write `.prove-it/contract.json` before your first edit (create the folder if needed).
If one exists from an earlier task, replace it.

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

- `task`: the user's request in one sentence. Do not narrow it to something easier.
- `scope`: the files you expect to change. Globs work: `src/auth/**`, `docs/`. Keep it tight.
- `criteria`: what must be true when you are done, one entry per requirement in the user's request.
  - `command`: a command that proves the criterion. Prefer one targeted test.
  - `fails_before: true`: the test must fail on the code before your change and pass after it.
    Use it for every bug fix and new behavior. This is what shows the test exercises your change.
    New or changed test files are carried over automatically; list others in `"tests": [...]`.
  - A criterion without a command is reported as not verified.

## 2. While working: stay on course

- Only edit files in scope. If the task really needs another file, add an amendment and say why:
  `"amendments": [{"scope": ["config/prices.py"], "reason": "the limit lives here"}]`.
  Amendments appear in the report; mention them to the user.
- Do not drop or weaken criteria once you have started editing. If you cannot meet one, keep it
  and report it as not met.
- Do not weaken tests: no deleting tests, removing assertions, or adding skip/only markers.
  If the task truly requires it (the behavior was removed), declare it:
  `"allow_test_changes": [{"file": "tests/test_old.py", "reason": "..."}]`.
- Do not edit `.prove-it.json`, `.prove-it/log.jsonl`, or Claude settings. Ask the user instead.

## 3. Before saying done: run the verifier

From the project root (on Windows, use `python` instead of `python3`):

```
python3 <skill folder>/scripts/prove_it.py
```

It reads the contract, runs the project's test, build and lint commands and every criterion,
checks every changed file against the scope, and checks test files for weakening.
Use `--base <commit>` if your work is already committed (the hook tells you which).

You can still add claims for anything else you want to state (`--claims .prove-it/claims.json`,
see the README). Without a contract, claims must list every changed file.

## 4. Act on the result

- `NOT SUPPORTED` criterion or claim: fix the work, or report it as not met. Never reword it to pass.
- "also passes on the code before the change": your test does not exercise the change. Write one that does.
- `OUT OF SCOPE`: undo the change, or add an amendment with a real reason.
- `TEST WEAKENED`: restore the test, or declare why in `allow_test_changes`.
- `FAIL` or `TIMEOUT` check: fix the cause and run again. If you cannot, say it fails.
- `UNVERIFIED` or `NOT_RUN`: say so. Never describe these as passing.

## 5. Report to the user

```
Task: <task>
Criteria:
  c1 <text>: met / not met / not verified (evidence: <command> passed; failed before the change)
Scope: all changes in scope | amended: <files> because <reason>
Not verified: <everything under NOT VERIFIED>
Failed: <anything that failed, or "nothing">
```

## Rules

- Quote results from the report. Never write evidence by hand or paraphrase a failure into a pass.
- Do not claim more than the report supports. "Tests pass" means the exact command in the report exited 0.
- A passing criterion shows what its command checks, not that everything is correct. Say what is not covered when it matters.
- If the script cannot run (no Python, no git), say that plainly and report the work as unverified.
- If the Prove it hook blocks an edit or a stop, do what it says: run the exact command it gives
  (including any `--base`) and report the result. Do not run with `--no-log`; the hook only accepts logged runs.
  Any edit, commit or contract change after a run means you must run it again.
