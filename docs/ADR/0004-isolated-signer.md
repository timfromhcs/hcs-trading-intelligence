# ADR 0004 — Isolated signer

Status: accepted.

## Context

Models, MCP clients and the REST layer must never hold signing authority. A hot key in a general-purpose process would turn any bug or prompt injection into a fund-loss path.

## Decision

Signing happens in a narrow component (`signing.py`, `keystore.py`). It accepts only a validated plan carrying the chain, wallet, asset, destination, amounts, risk decision ID and plan hash. It re-decodes the unsigned payload, checks it against the plan, refuses while the kill switch is set, tracks idempotency keys, and returns an unsupported error for chains without a verified signer. Keys are stored with scrypt and AES-256-GCM, with `wallet_id:address:chain` as associated data. No export, sweep or arbitrary-bytes signing exists.

## Consequences

- Adding a chain needs a decoder, test vectors and a security review. Placeholder chains cannot sign.
- Python memory cannot be guaranteed to be wiped, so the hot float must stay small and the reserve must live in a separate cold wallet. See [wallet security](../WALLET_SECURITY.md).
- Changes to signing code need the dedicated test suite and review by the code owners.
