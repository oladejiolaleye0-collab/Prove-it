# Security

## What Prove it runs

Prove it runs commands from the project it checks: test, build and lint commands it detects (`npm run test`, `make test` and so on), anything in `.prove-it.json`, and the commands in the agent's contract and claims. That is the point of the tool, and it means **you should only use it in repositories you trust**, the same as running their tests yourself.

`fails_before` creates a temporary git worktree outside the project and links folders such as `node_modules` into it. Links are removed before the worktree is deleted, so the originals are never touched.

Reports print the last lines of command output, which can include secrets your tests print. The log (`.prove-it/log.jsonl`) stores statuses, ids and claim text, and `.prove-it/` is ignored by git.

Prove it makes no network requests.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting on this repository (Security tab, "Report a vulnerability"), with steps to reproduce. You should get a reply within a week.
