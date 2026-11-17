# Wallet security

This guide explains how the application separates custody from automation. It describes the current code and states where protection is only a design intent. The [signing architecture](SIGNING.md) covers the signer interfaces and [WALLETS.md](WALLETS.md) covers read-only balance commands.

## Hot, cold and watch-only wallets

| Role | Key location | What the application can do |
|---|---|---|
| Cold reserve | Hardware wallet or offline device. Never on this machine | Nothing. The application never loads or sees this key |
| Hot trading wallet | Encrypted local keystore, or a venue account with API keys | Trade inside the policy envelope with a deliberately small float |
| Watch-only | No key. A public address only | Read balances and transaction status |

Wallet role metadata in snapshots, a hot-exposure risk check and a dashboard badge are tracked work in the [roadmap](ROADMAP.md). Today every wallet snapshot is read-only public data and the application does not label an address as hot or cold. Treat the table above as the operating rule, not as a software-enforced property.

### A reserve percentage is not custody

A software setting such as "keep 90% in reserve" cannot protect funds if the same key can sign everything. Malware, a vulnerable dependency or a compromised process would be able to move them. Only a separate cold wallet, whose key never enters this application, isolates the reserve. Fund the hot wallet with an amount you can afford to lose, and keep the rest elsewhere.

Starter policy values from the product brief, which you must review and set yourself, are no more than about 10% of intended holdings in the hot wallet, a per-trade risk budget near 0.25% of hot equity, a pause at a 1% daily hot loss and a 5% drawdown. These are examples, not advice, and they are not all enforced in code yet. The enforced pre-trade limits are in [EXECUTION.md](EXECUTION.md).

## Key custody

- Keys are never sent to a model, to MCP clients, to logs or to the REST API.
- The local keystore uses scrypt key derivation and AES-256-GCM. Associated data binds each record to `wallet_id:address:chain`, so a record cannot be swapped between wallets.
- A decrypted key exists in memory only while a signature is made. Python cannot guarantee that memory is zeroed, so a compromised host can still extract it. This is why the hot float must be small.
- The signer only accepts a validated transaction plan that matches an approved risk decision and a plan hash. It refuses while the kill switch is set. Chains without a verified signer return an unsupported error and never produce a signature.
- Seed creation, import and export are local operator actions. There is no key export, seed export, sweep, arbitrary transfer, arbitrary byte signing or withdrawal tool in the REST API or the MCP catalogue, and none may be added.

## Exchange API keys

- Create keys with the minimum scope: read and spot trade. Disable withdrawal and funding permissions at the venue.
- Keep key files outside the data directory and the repository. Pass them with `--kraken-live-credentials` or `--kraken-reconcile-credentials` only when you want those features.
- Revoke a key at the venue if you suspect exposure. The local `spot-canary-revoke` command removes the local authorization but does not revoke the venue key.

## Backup and recovery checklist

1. Write the seed phrase of every wallet you control on paper or metal, offline. The application does not store or back it up.
2. Verify recovery by restoring the seed on a spare device with a trivial amount before trusting a backup.
3. Store backups in at least two physical places. Never photograph, email or paste a seed.
4. Back up the application data directory to encrypted storage. It holds the database and audit history, not wallet custody.
5. Record the passphrase of the local keystore separately from the keystore file.
6. After any suspected compromise, set the kill switch with `hcs-trader pause-execution`, revoke exchange keys, then move funds with a trusted device using the recovery seed.
7. Never test on a mainnet wallet. Use regtest, devnet or testnet with throwaway keys.

## Node and RPC trust

Balance commands connect to a node you run on loopback. A node can lie or be out of sync, and the adapters pin chain identity (chain ID or genesis hash) to reduce wrong-network mistakes. A remote RPC provider can see every address you query and can return stale data. Use your own node for anything that matters.

## Known gaps

- UTXO adapters (BTC, LTC, DGB) read confirmed balances only. They have no transaction builder or PSBT signer, and spending is unsupported.
- EVM and Solana adapters inspect and allowlist transaction data but no live swap path is enabled in the service.
- No hardware wallet integration, no address derivation and no wallet role field yet.
- No wallet seed or key has been exercised on mainnet by this project.
