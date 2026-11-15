## Summary

<!-- What changes, and why. Link the issue or roadmap item. -->

## Checklist

- [ ] I read `AGENTS.md` and this change stays inside its V1 scope (spot-only, `PAPER` by default, no new agent permissions).
- [ ] Tests were added or updated first and cover the change, including failure and fail-closed paths.
- [ ] Local gates pass: `uv run --locked ruff check .`, `uv run --locked ruff format --check .`, `uv run --locked mypy`, `uv run --locked pytest`.
- [ ] Frontend changes pass `npm run lint`, `npm run typecheck` and `npm run build` in `frontend/`.
- [ ] Money and quantities use `Decimal` or integer base units.
- [ ] No secrets, seed phrases, private keys, API keys, real wallet addresses or personal transaction data are included.
- [ ] No test touches mainnet funds or a live trading venue; network tests use fixtures, regtest, devnet or testnet.
- [ ] Risk limits, security checks, type checks and coverage thresholds are not weakened; no test is skipped or marked xfail to pass CI.
- [ ] Changes to risk, signing, keystore, wallets, execution, MCP or workflows are called out for security review.
- [ ] Documentation describes only verified behaviour.

## Verification

<!-- Exact commands run and their results. -->
