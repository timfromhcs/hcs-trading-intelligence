# Read-only wallets

Run `hcs-trader eth-balance ADDRESS` to read a native ETH balance from an operator-managed node on `127.0.0.1:8545`. Use `--port` for another local port. Provide a lowercase 20-byte hex address with the `0x` prefix. The address is public. The command does not need an account unlocked at the node.

The adapter checks that the node reports chain ID 1, obtains a finalized block and requests the balance at that block's canonical hash. The node must support the EIP-1898 block identifier. The result includes the canonical native asset ID, block number and hash, local observation timestamp, and exact wei as a decimal string. This identity check relies on the operator's node and its sync state. [Ethereum JSON-RPC](https://ethereum.org/developers/docs/apis/json-rpc/) · [EIP-1898](https://eips.ethereum.org/EIPS/eip-1898)

The current command covers native ETH. Token balances, transaction history, address derivation, signing, broadcast and additional chain adapters are separate milestones.
