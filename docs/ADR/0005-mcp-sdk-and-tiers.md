# ADR 0005 — MCP SDK and capability tiers

Status: accepted.

## Context

AGENTS.md requires one MCP stack and enforcement at the tool execution boundary, not in prompts.

## Decision

Use the official MCP Python SDK on the v2 line (`mcp>=2.3,<3`) over stdio, without a second protocol layer. Every tool declares a tier (`READ`, `RESEARCH`, `PAPER_TRADE`, `LIVE_TRADE`, `ADMIN_LOCAL`). A guard checks the grant at call time, fails closed, and appends a hashed-argument record to `mcp_invocations.jsonl`. The default grant is `READ`, and all current tools are read-only. The server opens the database read-only.

## Consequences

- No side-effect tool or network transport is exposed. Streamable HTTP needs its own review.
- The SDK range is bumped only through the locked dependency review and a rerun of the MCP tests.
- The audit file is append-only from the application view, not tamper-proof.
