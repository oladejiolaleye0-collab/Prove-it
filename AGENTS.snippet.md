## Verify before saying "done"

Before you tell the user that work is done, fixed, working or passing, follow the
protocol in `prove-it/SKILL.md` (adjust the path to where Prove it is installed):

1. Write each claim you are about to make to `.prove-it/claims.json`.
2. Run `python3 prove-it/scripts/prove_it.py --claims .prove-it/claims.json`.
3. Report what was verified, what was not verified, and what failed, using the script's output.

Do not edit `.prove-it.json`, skip tests, or reword a claim to get past the check.
