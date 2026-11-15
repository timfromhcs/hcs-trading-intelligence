# Architecture

HCS Trading Intelligence is built as a local-first, modular monolith designed for self-custodial crypto research, paper trading, and tightly policy-bounded spot execution.

## System overview

```
+------------------------------------------------------------------------+
|                            Desktop UI                                  |
|         (React 19 + TypeScript + Vite, served locally on loopback)     |
+-----------------------------------+------------------------------------+
                                    |
                                    v HTTP (127.0.0.1:8765)
+------------------------------------------------------------------------+
|                          FastAPI REST Layer                            |
|             (Host header validation, local-origin CSRF checks)         |
+---------+------------------+------------------+------------------+-----+
          |                  |                  |                  |
          v                  v                  v                  v
+------------------+ +------------------+ +------------------+ +------------------+
| Market & News    | | Quant Research   | | Paper Ledger     | | Risk & Policy    |
| Quotes, candles, | | Indicators,      | | Balance, fills,  | | Pre-trade engine,|
| macro, Fed news  | | backtests,       | | cost-basis,      | | limits, spread/  |
| (Kraken/ECB/FRED)| | walk-forward     | | realized PnL     | | slippage caps    |
+---------+--------+ +---------+--------+ +---------+--------+ +---------+--------+
          |                    |                    |                    |
          +--------------------+--------------------+--------------------+
                                    |
                                    v
+------------------------------------------------------------------------+
|                   SQLite Audit & State Store (WAL)                     |
|           (Schema v12, versioned migrations, online backups)           |
+------------------------------------------------------------------------+
                                    |
          +-------------------------+-------------------------+
          |                                                   |
          v                                                   v
+------------------------------------+ +---------------------------------+
|          Isolated Signer           | |       Execution Adapters        |
| - Scrypt KDF + AES-256-GCM keystore| | - Kraken Spot (HMAC-SHA512)     |
| - AAD binding (wallet:address:chain| | - Solana Jupiter (Allowlisted)  |
| - Deterministic plan hash audit    | | - EVM Router (Exact allowance)  |
| - Ed25519 / SECP256k1 signing      | | - Fail-closed reconciliation    |
| - Zero secret export to LLM/MCP    | | - Withdrawals strictly disabled |
+------------------------------------+ +---------------------------------+
```

## Architectural principles

1. **Local-First & Loopback Binding:** The application runs on the operator's machine and binds strictly to `127.0.0.1`. All mutating state endpoints require a matching local origin to prevent cross-site request forgery.
2. **Deterministic Domain Core:** Financial calculations (RSI, indicators, PnL, cost-basis, pre-trade risk) use exact `Decimal` arithmetic. Language models assist only with summarization and research briefs; they cannot alter trade limits or issue signing commands.
3. **Custody & Signing Isolation:** Long-term reserves remain physically isolated in cold wallets outside the running application. Private keys for hot trading floats are stored encrypted using Scrypt KDF and AES-256-GCM with associated data binding. Signers independently decode and validate transaction payloads against pre-approved plans before generating signatures.
4. **Spot-Only, Fail-Closed Execution:** The execution engine strictly prohibits margin, borrowing, leverage, or bridge operations. If a submission encounters an ambiguous network response or timeout, it immediately transitions to `RECONCILIATION_REQUIRED` and halts further orders until venue reconciliation confirms state.
5. **Durable Kill Switch:** The emergency pause is persisted in SQLite across process restarts and crashes. When triggered, all new order submissions and signings are blocked.
6. **Portable Agent Skills:** Structured, versioned `SKILL.md` specifications in `src/hcs_trader/skills/` (`market-research`, `strategy-lab`, `portfolio-operator`, `risk-auditor`, `release-engineer`) define strict capability tiers, trusted/untrusted data boundaries, ordered workflows, stop conditions, and explicit forbidden actions.


