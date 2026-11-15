# Read-only wallets

Run `hcs-trader eth-balance ADDRESS` to read a native ETH balance from an operator-managed node on `127.0.0.1:8545`. Use `--port` for another local port. Provide a lowercase 20-byte hex address with the `0x` prefix. The address is public. The command does not need an account unlocked at the node.

The adapter checks that the node reports chain ID 1, obtains a finalized block and requests the balance at that block's canonical hash. The node must support the EIP-1898 block identifier. The result includes the canonical native asset ID, block number and hash, local observation timestamp, and exact wei as a decimal string. This identity check relies on the operator's node and its sync state. [Ethereum JSON-RPC](https://ethereum.org/developers/docs/apis/json-rpc/) · [EIP-1898](https://eips.ethereum.org/EIPS/eip-1898)

Run `hcs-trader sol-balance ADDRESS` for native SOL using a node on `127.0.0.1:8899`. The command validates a 32-byte base58 public address, compares the node's genesis hash with mainnet-beta and requests finalized commitment. It reports exact lamports as a decimal string and the returned slot. [Solana balance RPC](https://solana.com/docs/rpc/http/getbalance) · [Genesis hash RPC](https://solana.com/docs/rpc/http/getgenesishash)

Run `hcs-trader btc-balance ADDRESS --cookie-file PATH` for the confirmed UTXOs matching a public Bitcoin address. Use a synced Bitcoin Core mainnet node on `127.0.0.1:8332` with JSON-RPC 2.0 support. The command reads the node's local authentication cookie and validates the address with Core before requesting a single `addr(...)` descriptor scan. It reports the scan block and exact satoshis. An explicit scan can take time and uses the node's UTXO scanning facility. The result covers matching confirmed unspent outputs at the scan block, including any immature outputs returned by the node. [Bitcoin Core scan documentation](https://bitcoincore.org/en/doc/30.0.0/rpc/blockchain/scantxoutset/)

Token balances, transaction history, address derivation, signing, broadcast and additional chain adapters are separate milestones.
