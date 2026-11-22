---
name: portfolio-operator
description: Track paper balances, verify read-only wallet state, reconcile cost basis and monitor custody boundaries. Use for portfolio snapshots and reconciliation reviews.
metadata:
  version: "1.0.0"
  purpose: "Track portfolio balances, verify on-chain UTXO/account state, reconcile cost basis, and monitor custody boundaries"
  capability_tier: READ
  required_tools: "portfolio.get_paper_balance, wallets.get_snapshots"
---

# Portfolio Operator Skill

## Purpose & Triggers
- **Purpose**: Maintain accurate accounting of simulated paper balances and read-only self-custodial wallet balances across supported networks (SOL, BTC, ETH, LTC, DGB), enforce weighted-average cost basis, and verify the physical isolation of cold reserves.
- **Triggers**: Scheduled portfolio reconciliation cycle, user viewing wallet snapshots in dashboard, post-fill accounting update, or startup recovery.

## Capability Tier & Required Tools
- **Capability Tier**: `READ` (read-only custody inspection and accounting ledger).
- **Required MCP Tools**:
  - `portfolio.get_paper_balance`
  - `wallets.get_snapshots`

## Trusted and Untrusted Data Sources
- **Trusted Data**:
  - Local SQLite audit store tables `paper_fills` and `wallet_snapshots`.
  - Canonical network addresses and deterministic decimal balance conversions.
- **Untrusted External Data**:
  - Unauthenticated third-party RPC responses or block explorer APIs (must be validated against network genesis/chain ID and address checksums).

## Input and Output Schema
- **Inputs**:
  - `wallet_id` or `network`: e.g. `solana`, `ethereum`, `bitcoin`, `litecoin`, `digibyte`.
  - `limit`: Number of historical balance snapshots to retrieve ($1 \le \text{limit} \le 100$).
- **Outputs**:
  - Paper portfolio ledger: `{cash, position, average_entry_price, realized_pnl, unrealized_pnl}`
  - On-chain balance snapshots: `{network, address, asset, base_units, decimals, observed_at, block_height}`
  - Custody boundary status: `{cold_reserve_isolated: true, hot_wallet_exposure_within_bounds: true}`

## Ordered Workflow
1. Query `portfolio.get_paper_balance` to inspect current simulated capital and open positions.
2. Query `wallets.get_snapshots` to inspect observed public on-chain balances.
3. Verify asset identifiers against canonical registry (`chain:namespace:asset_id`).
4. Reconcile cost basis and realized PnL against verified fill events.
5. Confirm that cold reserve keys remain physically disconnected from running software.
6. Publish reconciled portfolio summary.

## Safe Defaults and Stop Conditions
- **Safe Defaults**: If RPC nodes are unreachable, display the last known balance snapshot tagged with `STALE` and its exact timestamp.
- **Stop Conditions**: If an unknown or unverified token contract attempts to register under an existing symbol, immediately halt and alert operator.

## Failure and Recovery
- If database read fails during startup, retry with exponential backoff before reporting storage error.
- Re-run reconciliation on restart to ensure zero duplicated fills or ledger drift.

## Evidence and Citation
- Every snapshot must record `address`, `network`, `asset`, `observed_at`, `block_number` (or `slot`), and `data_quality`.
- Clear distinction must be maintained between paper balance, on-chain hot wallet balance, and cold reserve custody.

## Explicit Forbidden Actions
- **FORBIDDEN**: Exporting private keys, seed phrases, or keystore passwords.
- **FORBIDDEN**: Requesting or executing arbitrary transfers, withdrawals, or token sweeps.
- **FORBIDDEN**: Treating cold reserves as spendable hot-wallet capital.
- **FORBIDDEN**: Altering cost basis or PnL entries retroactively.

## Acceptance Checks
- Test fixtures in `tests/test_paper.py` and `tests/test_wallets.py` pass.
- Idempotency test verifies that repeated queries or duplicate events never double-count balances or fees.
- Address derivation and checksum validation pass for all 5 supported chains.
