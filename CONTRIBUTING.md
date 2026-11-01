# Contributing

Thanks for helping. The most useful contributions right now:

1. **Reports from real sessions.** What did the agent do, what did Prove it say, and was it right? Open an issue with the prompt, the contract, and the report.
2. **False blocks and missed problems.** A hook that blocked good work, or let bad work through.
3. **More ecosystems.** Test command detection, test file patterns, and `fails_before` setups for languages that are not covered well yet.

## Development

Standard library only, Python 3.8+. No install step.

```bash
python3 -m unittest discover -s tests
```

Check that the plugin still validates if you change `.claude-plugin/` or `hooks/`:

```bash
claude plugin validate .
```

## Ground rules

- Keep it dependency-free and Python 3.8 compatible. CI runs Linux, macOS and Windows.
- Every behavior change comes with a test, and a bug fix with a test that fails without it. Prove it should pass its own `fails_before` bar.
- Never let a hook trap a session: on unexpected input or an internal error, hooks allow and say so.
- Keep messages to the agent actionable: say what is wrong and the exact way to fix it.
- Update `CHANGELOG.md` and the docs in the same pull request.
