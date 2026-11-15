# Threat Model

This document outlines the threat model, trust boundaries, attacker profiles, and implemented countermeasures for HCS Trading Intelligence.

## 1. Attacker profiles & assumptions

- **Malicious Remote Web Origin:** Attacker attempts CSRF, clickjacking, or loopback port probing from an arbitrary browser tab opened on the operator's machine.
- **Compromised Dependency / Supply Chain:** Malicious upstream Python or Node package attempts credential harvesting or remote code execution.
- **Malicious Prompt Injection / Untrusted Feeds:** Malicious content injected into news RSS feeds, macroeconomic headlines, or market descriptions attempting to influence LLM research outputs into recommending malicious actions or exceeding policy envelopes.
- **Malicious / Compromised RPC or DEX Route:** Malicious RPC endpoint returning spoofed account balances, malicious token mints, or altered transaction routes with drainer instructions.
- **Local Process Boundary:** The operator's machine is protected by standard OS permissions. However, LLM contexts and external tool layers are treated as untrusted boundaries.

## 2. STRIDE threat analysis & implemented mitigations

| Threat | Risk | Implemented Countermeasures |
|---|---|---|
| **Spoofing** | Fake market identifiers, spoofed tokens, lookalike contracts | Canonical `AssetId` and `MarketId` structures (`chain:namespace:asset_id`). Strict mint allowlists on Jupiter (`SOL`, `USDC`, `USDT`) and EVM (`WETH`, `USDC`). Tokens resolved from LLM outputs are strictly rejected. |
| **Tampering** | Altered transaction proposals, manipulated order payloads | Canonical SHA-256 plan hashing (`compute_plan_hash`). Signer independently decodes unsigned transaction payloads and verifies destination, amount, and chain against the pre-approved plan before signing. |
| **Repudiation** | Denial of trade proposals, altered trade history | Versioned SQLite audit event store (schema v12) tracking all order states, transition timestamps, and risk decision IDs. |
| **Information Disclosure** | Secret exposure, private key exfiltration to LLM or logs | Scrypt KDF + AES-256-GCM encrypted keystore. Associated data binding (`wallet_id:address:chain`) prevents record substitution. Decrypted keys exist only in short-lived memory during signing. Zero key-export or wallet-sweep tools in MCP or REST catalogue. |
| **Denial of Service** | Upstream provider outages, exchange timeouts, rate limits | Bounded rate limiting and timeouts. Stale quote detection ($\ge 30\text{s}$) automatically blocks new order creation. Fail-closed transition to `RECONCILIATION_REQUIRED` on ambiguous exchange responses. |
| **Elevation of Privilege** | LLM or MCP client bypassing risk limits, unlimited approvals | Pre-trade risk policy engine enforces hard bounds outside the model ($10–$1,000 notional, 0.5% max spread, 1.0% max slippage). Unlimited ERC-20 approvals (`2**256 - 1`) are rejected with `EVMAllowanceError`. Persistent kill switch blocks execution regardless of agent prompts. |

## 3. Residual risks & operational boundaries

- **Physical / Root Host Compromise:** An attacker with root / administrative access to the host OS or hardware can read memory or dump disk state. Long-term cold reserves must therefore remain in physically separate hardware/cold wallets whose keys never enter the machine.
- **Market & Liquidity Risks:** Bounded execution does not guarantee execution price during market gaps, sudden exchange halts, or stablecoin depegs.
- **Spot-Only Policy:** The software strictly forbids margin borrowing, leverage, options, lending, and bridges. Any attempt to introduce these requires a completely separate threat model review and audited contract allowlists.
