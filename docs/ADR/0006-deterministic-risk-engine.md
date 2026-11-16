# ADR 0006 — Deterministic risk engine

Status: accepted.

## Context

A model or strategy plugin must not be able to approve its own trade.

## Decision

`RiskEngine` evaluates a typed `RiskPolicy` before any order submission. It has no model or network imports. It checks the kill switch, live mode, venue and market allowlists, strategy version, quote freshness, notional bounds, spread and slippage, and open orders. Every decision is returned with a decision ID and persisted. A rejection is a normal outcome. The kill switch is stored in SQLite and survives restarts. MCP resources render limits from the same policy defaults, so documentation cannot drift from enforcement.

## Consequences

- Changing a limit is a code change with tests, not a prompt or a setting a model can reach.
- Hot-wallet exposure and loss-based pauses are not enforced yet and are listed in the roadmap.
