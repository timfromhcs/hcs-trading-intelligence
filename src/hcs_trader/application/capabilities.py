"""Honest per-chain capability matrix (AGENTS.md 1.3).

The matrix is derived from the signer's own capability report so the UI, REST
API and MCP resource cannot claim more than the signer can do.
"""

from pydantic import BaseModel

from hcs_trader.domain.signing import signing_capabilities


class ChainCapability(BaseModel):
    chain: str
    symbol: str
    signing_namespace: str
    wallet_scope: str
    execution_scope: str
    direct_onchain_execution: bool
    live_execution_enabled: bool
    signing_chain_valid: bool
    signing_note: str


# (chain, symbol, signing namespace, wallet scope, execution scope)
_CHAINS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "Bitcoin",
        "BTC",
        "bip122",
        "Read-only address balance adapter (local node RPC)",
        "Kraken BTC/USD spot canary only; off by default; no direct on-chain spend",
    ),
    (
        "Ethereum",
        "ETH",
        "eip155",
        "Read-only account balance adapter (chain ID pinned)",
        "EVM swap adapter is preview and test-fixture only; no live route enabled",
    ),
    (
        "Solana",
        "SOL",
        "solana",
        "Read-only SOL balance adapter",
        "Jupiter adapter is preview and test-fixture only; no live route enabled",
    ),
    (
        "Litecoin",
        "LTC",
        "bip122",
        "Read-only address balance adapter (local node RPC)",
        "No execution route; direct spend disabled",
    ),
    (
        "DigiByte",
        "DGB",
        "bip122",
        "Read-only address balance adapter (local node RPC)",
        "No execution route; direct spend disabled",
    ),
)


def chain_capability_matrix() -> list[ChainCapability]:
    signing = signing_capabilities()
    rows: list[ChainCapability] = []
    for chain, symbol, namespace, wallet_scope, execution_scope in _CHAINS:
        capability = signing[namespace]
        rows.append(
            ChainCapability(
                chain=chain,
                symbol=symbol,
                signing_namespace=namespace,
                wallet_scope=wallet_scope,
                execution_scope=execution_scope,
                direct_onchain_execution=False,
                live_execution_enabled=False,
                signing_chain_valid=capability.chain_valid,
                signing_note=capability.note,
            )
        )
    return rows
