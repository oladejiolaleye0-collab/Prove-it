---
name: prove-it
description: Use before telling the user that coding work is done, fixed, working, or passing. Runs the project's real tests, build and lint, checks each claim you are about to make against the git diff, and reports what was verified and what was not.
---

# Prove it

Do not tell the user a task is done, fixed, or passing until this skill has run and
you have reported its result. Unverified "done" claims are the failure this skill exists to stop.

## Steps

1. **Finish the work.** Do not run this halfway.

2. **List your claims.** Write every claim you are about to make to `.prove-it/claims.json`
   (create the folder if needed). Use one entry per claim:

   ```json
   [
     {"claim": "Fixed the login redirect bug", "files": ["src/auth.py"], "symbols": ["redirect_after_login"]},
     {"claim": "All tests pass", "kind": "tests_pass"},
     {"claim": "Build succeeds", "kind": "build_ok"}
   ]
   ```

   - `files`: files you say you changed. Checked against the git diff.
   - `symbols`: functions, classes or names you say you added or changed. Checked against lines added in the diff.
   - `kind`: `tests_pass`, `build_ok` or `lint_ok` ties the claim to a real command run.
   - `command`: a command that demonstrates this claim, such as one targeted test. Supported only if it exits 0.
     Use it for behavior claims ("negative prices are rejected") that file and symbol checks cannot show.
   - A claim with none of these cannot be checked and will be reported as unverified. Prefer claims that can be checked.
   - List every file you changed in some claim's `files`. Unclaimed changes are reported, and the hook blocks on them.

3. **Run the verifier** from the project root. The script is in this skill's folder:

   ```
   python3 <skill folder>/scripts/prove_it.py --claims .prove-it/claims.json
   ```

   On Windows, use `python` instead of `python3`.
   Use `--base <branch>` if your work is already committed (for example `--base main`).
   Use `--strict` if the user wants unverified items treated as failures.

4. **Act on the result.**
   - `NOT SUPPORTED` claim: the evidence contradicts you. Fix the work or remove the claim. Do not reword it to slip past the check.
   - `FAIL` or `TIMEOUT` check: fix the cause and run again. If you cannot fix it, tell the user it fails.
   - `UNVERIFIED` item or `NOT_RUN` check: say so. Never describe these as passing.
   - Files under "changed but not claimed": mention them or explain why they changed.

5. **Report to the user** in this shape:

   ```
   Verified: <claims with SUPPORTED, plus the commands that passed>
   Not verified: <everything under NOT VERIFIED in the report>
   Failed: <anything that failed, or "nothing">
   ```

## Rules

- Quote results from the report. Never write evidence by hand or paraphrase a failure into a pass.
- Do not edit `.prove-it.json`, skip or delete tests, or change test commands to make the run pass.
  If a command needs changing, ask the user.
- Do not claim more than the report supports. "Tests pass" means the exact command in the report exited 0.
- A passing check is not proof the change is correct. Say what the checks do not cover when it matters.
- If the script cannot run (no Python, no git), say that plainly and report the work as unverified.
- If the Prove it hook blocks you, do what it says: run the exact command it gives (including any `--base`) and report the result.
  Do not run with `--no-log`, because the hook only accepts logged runs. Any edit or commit after a run means you must run it again.
