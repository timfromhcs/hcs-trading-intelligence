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

## Independent payload decoding

The signer independently decodes the raw unsigned payload bytes. It asserts that destination, chain identifier, amount, and signer match the approved plan. Any discrepancy raises a `SignerTamperingError` and aborts the operation.

## MCP boundary and access controls

In accordance with execution invariants:
- The MCP server exposes no tools or resources for private key export, wallet sweeping, seed extraction, or arbitrary funds transfer.
- The model and conversational agents cannot inspect signer secrets or expand policy envelopes.
