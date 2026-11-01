## Prove your work and stay on task

Follow the protocol in `prove-it/SKILL.md` (adjust the path to where Prove it is installed):

1. Before editing, write `.prove-it/contract.json`: the task, the files in scope, and acceptance
   criteria, each with a command that proves it (`"fails_before": true` for bug fixes and new behavior).
2. Edit only files in scope. If you need more, add an amendment with a reason. Never drop criteria
   or weaken tests to make things pass.
3. Run `python3 prove-it/scripts/prove_it.py` (use `python` on Windows).
4. Report each criterion as met, not met or not verified, plus scope amendments, Not verified and
   Failed, using the script's output.

Do not edit `.prove-it.json`, skip tests, or reword a claim to get past the check.
