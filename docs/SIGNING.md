# Isolated signing and keystore

Phase 7 establishes the isolated transaction preview and cryptographic signing boundary. Agents and MCP tools never receive raw keys, seeds, or unrestricted signing capabilities.

## Software keystore

Private keys are protected in local storage using memory-hard key derivation and authenticated encryption:
- Key derivation: Scrypt (`N=16384`, `r=8`, `p=1`, 32-byte key) with a cryptographically secure 16-byte random salt.
- Encryption: AES-256-GCM with a 12-byte random nonce and associated data binding the wallet identifier, public address, and chain namespace.
- Tamper detection: Any modification to ciphertext, salt, or associated metadata causes immediate decryption rejection.
- Lifecycle: Decrypted secrets exist only in narrow, short-lived scopes during signature generation.

## Policy contract and plan validation

The signer accepts only `ValidatedTransactionPlan` instances carrying deterministic transaction boundaries:
- Canonical chain ID and network identity.
- Exact asset input and minimum output base units.
- Program/router destination identifier.
- Token mint identifiers for inputs and outputs.
- Maximum slippage (basis points) and maximum fee constraints.
- Simulation success flag and block reference.
- Expiration deadline with mandatory UTC offset.
- Plan hash: SHA-256 digest over the canonical, deterministic JSON serialization of the plan parameters.

Before generating any signature, `SignerPolicy` enforces that:
1. The global kill switch is inactive.
2. Real-money live execution is explicitly enabled.
3. Simulation succeeded prior to submission.
4. Target chain, destination router, and token mints are on the strict allowlist.
5. Max slippage and network fees do not exceed policy caps.
6. The transaction deadline has not expired.
7. The plan hash matches the recomputed canonical hash.

## Persisted pause and risk-decision binding

`IsolatedSigner` is constructed with two injected read-only callables and never imports storage directly:
- `pause_state() -> bool`: read at every `sign_transaction` call (wire it to `AuditStore.execution_pause().paused`). A `True` result, any non-`False` value, or an exception rejects signing (fail closed). A pause recorded after the signer was built still blocks signing.
- `risk_decision_lookup(decision_id) -> ApprovedRiskDecision | None`: the plan's `risk_decision_id` must resolve to a persisted decision with the same id, `allowed=True`, and a `plan_hash` equal to the plan being signed. Unknown, rejected, mismatched or failing lookups reject signing.

The signer also refuses to sign the same `idempotency_key` twice. A key is recorded only after a successful signature, so a failed attempt (for example a wrong passphrase) does not consume it. This in-memory guard is scoped to one signer instance; durable de-duplication across restarts remains the responsibility of the persisted order/transaction lifecycle.

The static `SignerPolicy.kill_switch_active` and `live_mode_enabled` flags remain as an additional immutable envelope; they do not replace the persisted pause check.

## Chain-valid signing status (placeholders)

Chain-valid signing exists for EVM (Sepolia, see below) only; UTXO and Solana signing remain placeholders. `signing_capabilities()` reports this per namespace:

| Namespace | Chain-valid | Behaviour |
|---|---|---|
| `eip155` (EVM) | Yes (Sepolia) | EIP-1559 type 0x02 signing with independent RLP/ABI decoding; mainnet disabled by default. Does not depend on `allow_test_only_signing`. A JSON envelope payload is rejected. |
| `bip122` (BTC/LTC/DGB) | No | Same placeholder. Not sighash/PSBT. Raises `SigningUnsupported` unless `allow_test_only_signing=True`. |
| `solana` | No | Ed25519 over the supplied bytes. The payload is a JSON test envelope, not a serialized Solana message, so the output is not broadcastable. |
| any other | No | Raises `SigningUnsupported`. |

`allow_test_only_signing` must only be set by tests. Signed output from any path must not be broadcast.

## Independent payload decoding

The signer independently decodes the raw unsigned payload bytes. It asserts that destination, chain identifier, amount, and signer match the approved plan. Any discrepancy raises a `SignerTamperingError` and aborts the operation.

## MCP boundary and access controls

In accordance with execution invariants:
- The MCP server exposes no tools or resources for private key export, wallet sweeping, seed extraction, or arbitrary funds transfer.
- The model and conversational agents cannot inspect signer secrets or expand policy envelopes.

## EVM (Sepolia) chain-valid signing (KL-002 track)

`src/hcs_trader/evm_signing.py` replaces the generic ECDSA-over-bytes path for the `eip155` namespace only. Solana and other namespaces are unchanged.

- Payload: unsigned EIP-1559 bytes `0x02 || rlp([chainId, nonce, maxPriorityFee, maxFee, gas, to, 0, data, []])`, built from the typed plan (`build_swap_payload`, `build_approve_payload`).
- The signer re-decodes the raw bytes (strict RLP, strict ABI, canonical re-encoding) before touching any key, then signs with `eth-account` using fields taken from the decoded payload, and re-recovers the signer address afterwards.
- Allowed chains: Sepolia (11155111) only. `EvmSigningPolicy.allow_mainnet` defaults to `False`; enabling it additionally requires operator-supplied mainnet routers and tokens.
- Allowed shapes: (1) exact-amount `approve(spender, amount)` where `to` is the plan's `token_in_mint`, spender is an allowlisted router, `amount == amountIn`, never unlimited; (2) SwapRouter02 `multicall(deadline, [exactInputSingle])` with tokens, `recipient == signer`, `amountIn`, `amountOutMinimum`, deadline and nonce all equal to the plan, `sqrtPriceLimitX96 == 0`, fee tier in {100, 500, 3000, 10000}. Bare `exactInputSingle` is rejected because SwapRouter02 binds no deadline in it. `value` must be 0, no access list, no contract creation.
- Fee caps and units: `maxFee * gas <= plan.max_network_fee_base_units` (a total, in wei), while `maxPriorityFeePerGas <= plan.max_priority_fee_base_units` (a per-gas price in wei, not a total). Gas <= `EvmSigningPolicy.max_gas_limit`. A planner must supply `max_priority_fee_base_units` as wei per gas for `eip155` plans.
- Idempotency, pause and risk binding: the EVM path runs after the shared pause, policy, persisted risk-decision and idempotency checks. A repeated `idempotency_key` is always refused (no cached replay is returned), and every call decodes the payload independently before any key access. A failed attempt does not consume the key.
- Plan intent: the plan has no declared kind. A plan whose `destination_id` equals its `token_in_mint` is signed as an approve and any other allowlisted router destination as a swap; the exact-amount and allowlist checks bound both, but the plan hash does not bind the intent explicitly.
- Addresses: SwapRouter02 `0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E` and Sepolia WETH `0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14` verified against Uniswap's official v3 deployments page. Sepolia USDC `0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238` was already in the in-repo `evm_swap` registry; it was not checked against Circle's or Uniswap's pages by this change and no test proves it.
- Limitations: no broadcast, simulation or gas estimation here (plan supplies `simulation_success`); native-ETH-in swaps unsupported (WETH only); tests are offline (no testnet rehearsal yet); the roundtrip test checks determinism, sender recovery and the Hardhat key to address mapping, not an external signed-transaction fixture. Idempotency durability follows the shared signer ledger (in-memory unless a ledger is injected).

## Solana chain-valid signing (KL-002 track)

`src/hcs_trader/solana_signing.py` produces chain-valid Solana signatures using `solders==0.29.0` (Apache-2.0, pinned). Entry point: `IsolatedSigner.sign_solana_transaction(plan, message_bytes, passphrase, at)`. The legacy `IsolatedSigner.sign_transaction` JSON-envelope path rejects every `solana:*` plan (`SigningUnsupported`), so it cannot be used to bypass the message checks.

Flow:
1. Shared pause check, `SignerPolicy.validate_plan` (kill switch, live mode, simulation, allowlists including `token_in_mint`, fee/slippage caps, expiry, plan hash), persisted approved risk decision and the signed-plan check run first, in the same order as the EVM path.
2. The legacy/v0 message is decoded independently and must re-serialize byte-for-byte (at most 1232 bytes).
3. The signer must be the fee payer and the only required signature. Programs are limited to System transfer, SPL Token `TransferChecked` (plus `SyncNative` and `CloseAccount` only in Jupiter mode), ATA `CreateIdempotent`, Compute Budget (limit and price) and Jupiter v6 (Jupiter mode only). Base fee (5000 lamports/signature) plus priority fee must fit the plan fee caps; a price requires an explicit limit.
4. Plain transfers must match plan destination, amount and `token_in_mint`. An ATA creation must derive from its owner and mint and may only create the plan's destination account for the plan's mint, so the signer cannot be made to fund rent for other accounts.
5. Jupiter mode is a property of the signer (`IsolatedSigner(solana_policy=SolanaMessagePolicy(allow_jupiter=True))`), not of the call. Messages must target the Jupiter program; System transfers are only allowed as wrap-SOL funding of the signer's own WSOL account, for a WSOL input, up to the plan amount; ATA creations must be for the signer's own wallet and the plan's `token_in_mint` or `token_out_mint`.
6. The 32-byte seed is decrypted only inside the call, must derive `plan.signer_address`, and the signature is verified before the wire transaction is returned. `tx_hash` is the base58 signature, which is the Solana transaction id.
7. A repeated `idempotency_key` is always refused (no cached replay). With an injected signed-plan ledger (the audit store) this survives restarts; without one it is process memory only. A failed attempt does not consume the key.

Limitations: Jupiter route data is opaque, so swap amounts, pools, token movements and output accounts are not bound by the signer (only simulation or post-trade reconciliation could verify them, and neither is wired in); address table lookups are accepted only for Jupiter and are not resolved (accounts they reference make the message fail closed); blockhash freshness and cluster identity cannot be checked offline; unchecked SPL `Transfer`, `Approve`, `SetAuthority` and Token-2022 are rejected; `jupiter_swap.py` route validation is not yet wired into this signing path; the devnet rehearsal test (`HCS_SOLANA_DEVNET_RPC`) has not been run, so on-chain acceptance is unverified. Tests: `tests/test_solana_signing.py`.
