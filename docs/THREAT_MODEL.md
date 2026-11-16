# Threat model

This document lists trust boundaries, attackers, mitigations and the tests that verify them. Where no test exists the entry says so. Numeric limits and the schema version are defined in code (`risk.py`, `storage.py`) and are not repeated here, so this text cannot silently go stale. A drift test checks that every test file named below exists.

## 1. Assets and trust boundaries

- **Assets:** hot-wallet keys and exchange API keys, the SQLite state and audit history, risk policy and kill-switch state, the integrity of market and news data.
- **Trusted:** the operator, the deterministic risk engine, the isolated signer, the operating system account.
- **Untrusted:** all network data (quotes, news, RPC replies), model output, MCP client requests, article text, webpages, filenames.
- **Boundary rule:** every action is authorized in deterministic code outside the model, through one path regardless of entry point.

## 2. Attacker profiles

- A malicious web page in a browser on the operator's machine (cross-site requests, loopback probing).
- A compromised dependency, build action or release workflow.
- Content authors who inject instructions into news, feeds or market descriptions.
- A malicious or faulty RPC, exchange API or swap route.
- A malicious or over-privileged MCP client.
- Malware with the operator's privileges. This is a residual risk (section 5).

## 3. STRIDE summary

| Threat | Risk | Countermeasures | Verifying tests |
|---|---|---|---|
| Spoofing | Lookalike tokens and fake markets | Canonical asset and market identifiers. Mint and contract allowlists. Tokens are never resolved from model output | `tests/test_domain.py`, `tests/test_jupiter_swap.py`, `tests/test_evm_swap.py` |
| Tampering | Altered order or transaction payload | Canonical plan hash. The signer decodes the payload and compares destination, amount and chain with the approved plan | `tests/test_signing.py` |
| Repudiation | Denied or altered trade history | Append-only audit events and order lifecycle records with risk decision IDs. MCP calls are logged with hashed arguments | `tests/test_storage_api.py`, `tests/test_mcp.py` |
| Information disclosure | Keys or secrets in logs, prompts or APIs | Encrypted keystore with bound associated data. No export, sweep or raw-signing tool in REST or MCP. Hidden prompt for the FRED key | `tests/test_signing.py`, `tests/test_cli.py`, `tests/test_fred.py` |
| Denial of service | Provider outage or rate limit | Bounded refresh, backoff, stale-quote detection, ambiguous results become `RECONCILIATION_REQUIRED` | `tests/test_quote_service.py`, `tests/test_kraken.py`, `tests/test_kraken_spot.py` |
| Elevation of privilege | A client or model bypasses limits | Deterministic pre-trade policy, durable kill switch, unlimited approvals rejected, MCP tiers enforced at call time | `tests/test_risk.py`, `tests/test_evm_swap.py`, `tests/test_mcp.py` |

## 4. Threats from the product brief

| Threat | Mitigation | Test coverage |
|---|---|---|
| Malicious MCP client | Capability tiers checked on every call, default grant is read-only, schema validation, invocation audit that fails closed | `tests/test_mcp.py` |
| Prompt injection in news or feeds | Feed text is data. Only titles and links of official hosts are stored. Model output is schema-validated and never authorizes an action. The model cannot reach the signer | `tests/test_fed_news.py`, `tests/test_local_research.py`. No injection corpus yet (gap) |
| Model hallucination | Indicators are deterministic. Malformed model output becomes `NO_TRADE`. Local model use is optional | `tests/test_local_research.py`, `tests/test_model_manifest.py` |
| Malicious RPC or stale or forked chain data | Chain ID or genesis pinning, finalized and canonical block checks, a wrong network is rejected, observation time is shown | `tests/test_wallets.py` |
| UTXO placeholder risk | BTC, LTC and DGB adapters are read-only. The signer returns unsupported for them. A node's reported balance is trusted | `tests/test_wallets.py`, `tests/test_signing.py` |
| EVM placeholder risk | Router inspection and exact allowances only. No live path. Unlimited approval rejected | `tests/test_evm_swap.py` |
| Exchange credential scope | Withdrawals not in the adapter. Key files are passed by path and not stored in the database. Operator must grant minimum scope at the venue | `tests/test_kraken_spot.py`. Venue scopes cannot be verified by code |
| API exposed to the LAN or internet | Loopback bind, Host header validation, local-origin checks on `POST` | `tests/test_storage_api.py`, `tests/test_paper.py` |
| Duplicate order after crash or timeout | Stable client order IDs, idempotency keys, ambiguous outcomes move to `RECONCILIATION_REQUIRED` and block new orders, startup recovery | `tests/test_kraken_spot.py`, `tests/test_signing.py` |
| Database corruption or partial write | Transactional writes, versioned migrations that refuse newer schemas, online backup | `tests/test_storage_api.py`. No disk-full or corruption injection yet (gap) |
| Upstream data revision | Observation date, source snapshot date and receipt time are stored separately, first receipt retained | `tests/test_ecb.py`, `tests/test_fred.py` |
| Dependency and CI compromise | Locked dependencies, pinned action SHAs, minimal workflow permissions, CodeQL, Scorecard, Dependabot, license inventory | CI workflows. No SBOM or provenance yet (gap) |
| Holdout contamination | Commitment hash and frozen candidates | `tests/test_promotion.py`, `tests/test_backtest.py` |

## 5. Wallet and signer STRIDE

| Category | Threat | Control |
|---|---|---|
| Spoofing | Signer asked to sign for a different wallet or chain | Plan carries wallet ID, address and chain. Keystore associated data binds the same triple |
| Tampering | Transaction changed between preview and signing | Plan hash is compared. The signer decodes the bytes itself |
| Repudiation | Signing without a record | Signing needs an approved risk decision ID and an idempotency key. Both are persisted |
| Information disclosure | Key leaves the signer | No export path. The key is decrypted only per signature. Python memory is not guaranteed to be wiped |
| Denial of service | Signer used to burn fees | Pause check, idempotency, risk limits |
| Elevation of privilege | Agent obtains signing or widens the allowlist | The signer is not an MCP resource or tool. Allowlists are code and change only through review |

Placeholder chains (UTXO and unreviewed EVM routes) return an unsupported error instead of signing. Adding one needs its own decoder, vectors and review.

## 6. Residual risks

- A compromised host or administrator can read memory, files and the keystore passphrase. Keep the reserve in a cold wallet that never touches this machine ([wallet security](WALLET_SECURITY.md)).
- Bounded execution does not set execution price in gaps, halts or depegs.
- The audit trail is not tamper-proof against a local administrator.
- An operator-run node can lie. A public RPC can also observe queried addresses.
- Spot only. Margin, leverage, options, lending, staking and bridges need a new threat model before any work starts.
- Tests use fixtures and local nodes. Mainnet signing and live swaps are unverified.

## 7. Review rules

Update this document with any change to wallet, signer, execution, MCP authorization, remote access or release workflow.
