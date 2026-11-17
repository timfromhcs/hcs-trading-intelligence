"""ChainWallet adapters, UTXO tx status and token balances against fake RPC nodes."""

import json
from collections.abc import Callable

import httpx2
import pytest

from hcs_trader.domain.wallet_port import (
    ChainWallet,
    SignedTransaction,
    TransferOrSwapIntent,
    UnsignedTransaction,
    WalletOperationUnsupported,
)
from hcs_trader.jupiter_swap import base58_encode
from hcs_trader.wallets import (
    SOL_GENESIS,
    UTXO_NETWORKS,
    EthereumChainWallet,
    LocalWalletRpc,
    SolanaChainWallet,
    UtxoChainWallet,
    WalletUnavailable,
)

_ETH_ADDR = "0x" + "12" * 20
_BLOCK = "0x" + "34" * 32
_TXID = "ab" * 32
_BLOCK_HASH = "cd" * 32
_USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48".lower()
_SOL_ADDR = base58_encode(bytes(range(1, 33)))
_SOL_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
_BTC_ADDR = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"

Handler = Callable[[list[object]], object]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _RpcError:
    def __init__(self, code: int) -> None:
        self.code = code


def _transport(
    handlers: dict[str, Handler], *, version: str = "2.0"
) -> httpx2.MockTransport:
    def node(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        outcome = handlers[body["method"]](body["params"])
        envelope: dict[str, object] = {"id": body["id"]}
        if version == "2.0":
            envelope["jsonrpc"] = "2.0"
        else:
            envelope["error"] = None
        if isinstance(outcome, _RpcError):
            envelope["error"] = {"code": outcome.code, "message": "not found"}
            if version == "1.0":
                envelope["result"] = None
        else:
            envelope["result"] = outcome
        return httpx2.Response(200, json=envelope)

    return httpx2.MockTransport(node)


# --- Port behaviour ------------------------------------------------------------


@pytest.mark.anyio
async def test_spend_operations_are_unsupported_on_every_adapter() -> None:
    async with httpx2.AsyncClient(transport=_transport({})) as client:
        rpc = LocalWalletRpc(client, 8545)
        wallets: list[ChainWallet] = [
            EthereumChainWallet(rpc, [_ETH_ADDR]),
            SolanaChainWallet(rpc, [_SOL_ADDR]),
            UtxoChainWallet(rpc, "btc-balance", [_BTC_ADDR]),
        ]
        for wallet in wallets:
            addresses = await wallet.get_addresses()
            assert addresses and addresses[0].role == "watch_only"
            with pytest.raises(WalletOperationUnsupported):
                await wallet.build_preview(
                    TransferOrSwapIntent(wallet.network, "x", 1, "y")
                )
            with pytest.raises(WalletOperationUnsupported):
                await wallet.simulate(UnsignedTransaction(wallet.network, b"", "h"))
            with pytest.raises(WalletOperationUnsupported):
                await wallet.broadcast(SignedTransaction(wallet.network, b""))


def test_adapters_reject_bad_construction() -> None:
    rpc = LocalWalletRpc(httpx2.AsyncClient(), 8545)
    with pytest.raises(ValueError):
        EthereumChainWallet(rpc, [])
    with pytest.raises(ValueError, match="EIP-55"):
        EthereumChainWallet(rpc, ["0x5AAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"])
    with pytest.raises(ValueError):
        EthereumChainWallet(rpc, [_ETH_ADDR], network="goerli")
    with pytest.raises(ValueError):
        SolanaChainWallet(rpc, ["short"])
    with pytest.raises(ValueError):
        UtxoChainWallet(
            rpc, "btc-balance", ["bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5"]
        )
    with pytest.raises(ValueError):
        UtxoChainWallet(rpc, "doge-balance", ["D" * 30])


# --- Ethereum -----------------------------------------------------------------


def _eth_handlers(
    *, decimals: int | None = None, balance: int = 2_500_000, chain_id: str = "0x1"
) -> dict[str, Handler]:
    def call(params: list[object]) -> object:
        tx, pinned = params
        assert isinstance(tx, dict)
        assert pinned == {"blockHash": _BLOCK, "requireCanonical": True}
        data = str(tx["data"])
        if data == "0x313ce567":
            real = 6 if tx["to"] == _USDC else 18
            return "0x" + (decimals or real).to_bytes(32, "big").hex()
        assert data.startswith("0x70a08231" + "0" * 24 + "12" * 20)
        if tx["to"] != _USDC:
            return "0x" + (0).to_bytes(32, "big").hex()
        return "0x" + balance.to_bytes(32, "big").hex()

    return {
        "eth_chainId": lambda _: chain_id,
        "eth_getBlockByNumber": lambda _: {"number": "0x100", "hash": _BLOCK},
        "eth_getBalance": lambda _: hex(10**18),
        "eth_call": call,
    }


@pytest.mark.anyio
async def test_ethereum_wallet_reads_native_and_allowlisted_tokens() -> None:
    async with httpx2.AsyncClient(transport=_transport(_eth_handlers())) as client:
        wallet = EthereumChainWallet(LocalWalletRpc(client, 8545), [_ETH_ADDR])
        balances = await wallet.get_balances()
        health = await wallet.health()
    by_asset = {b.asset: b for b in balances}
    assert len(balances) == 4  # ETH + WETH + USDC + UNI
    assert by_asset["eip155:1/native:ETH"].base_units == str(10**18)
    usdc = by_asset[f"eip155:1/erc20:{_USDC}"]
    assert usdc.base_units == "2500000" and usdc.decimals == 6
    assert usdc.height == 256 and usdc.reference == _BLOCK
    assert health.healthy and health.height == 256 and wallet.network == "eip155:1"


@pytest.mark.anyio
async def test_ethereum_token_decimals_mismatch_and_unlisted_token_fail() -> None:
    async with httpx2.AsyncClient(
        transport=_transport(_eth_handlers(decimals=18))
    ) as client:
        rpc = LocalWalletRpc(client, 8545)
        with pytest.raises(WalletUnavailable, match="decimals"):
            await rpc.ethereum_token_balance(_ETH_ADDR, _USDC)
        with pytest.raises(ValueError, match="allowlist"):
            await rpc.ethereum_token_balance(_ETH_ADDR, "0x" + "11" * 20)
        with pytest.raises(ValueError):
            await rpc.ethereum_token_balance("0xABC", _USDC)


@pytest.mark.anyio
async def test_ethereum_wrong_chain_and_garbage_call_results_are_unhealthy() -> None:
    handlers = _eth_handlers(chain_id="0x89")
    async with httpx2.AsyncClient(transport=_transport(handlers)) as client:
        health = await EthereumChainWallet(
            LocalWalletRpc(client, 8545), [_ETH_ADDR]
        ).health()
    assert not health.healthy and health.height is None
    garbage = _eth_handlers()
    garbage["eth_call"] = lambda _: "0x12"
    async with httpx2.AsyncClient(transport=_transport(garbage)) as client:
        with pytest.raises(WalletUnavailable, match="eth_call"):
            await LocalWalletRpc(client, 8545).ethereum_token_balance(_ETH_ADDR, _USDC)


@pytest.mark.anyio
async def test_ethereum_transaction_unknown_maps_to_none() -> None:
    handlers = _eth_handlers()
    handlers["eth_getTransactionReceipt"] = lambda _: None
    async with httpx2.AsyncClient(transport=_transport(handlers)) as client:
        wallet = EthereumChainWallet(LocalWalletRpc(client, 8545), [_ETH_ADDR])
        assert await wallet.get_transaction(_BLOCK) is None


# --- Solana -------------------------------------------------------------------


def _sol_handlers(accounts: list[dict[str, object]]) -> dict[str, Handler]:
    return {
        "getGenesisHash": lambda _: SOL_GENESIS,
        "getBalance": lambda _: {"context": {"slot": 77}, "value": 3_000_000_000},
        "getSlot": lambda _: 90,
        "getTokenAccountsByOwner": lambda params: {
            "context": {"slot": 78},
            "value": accounts if params[1] == {"mint": _SOL_USDC} else [],
        },
    }


def _token_account(mint: str, amount: str, decimals: int = 6) -> dict[str, object]:
    return {
        "pubkey": "x",
        "account": {
            "data": {
                "program": "spl-token",
                "parsed": {
                    "type": "account",
                    "info": {
                        "mint": mint,
                        "tokenAmount": {"amount": amount, "decimals": decimals},
                    },
                },
            }
        },
    }


@pytest.mark.anyio
async def test_solana_wallet_sums_token_accounts_per_allowlisted_mint() -> None:
    accounts = [_token_account(_SOL_USDC, "1500000"), _token_account(_SOL_USDC, "500")]
    async with httpx2.AsyncClient(transport=_transport(_sol_handlers(accounts))) as c:
        wallet = SolanaChainWallet(LocalWalletRpc(c, 8899), [_SOL_ADDR])
        balances = await wallet.get_balances()
        health = await wallet.health()
    by_asset = {b.asset: b for b in balances}
    usdc = next(b for a, b in by_asset.items() if a.endswith(f"/spl:{_SOL_USDC}"))
    assert usdc.base_units == "1500500" and usdc.decimals == 6 and usdc.height == 78
    assert len(balances) == 3  # SOL + USDC + USDT (empty)
    assert health.healthy and health.height == 90


@pytest.mark.anyio
@pytest.mark.parametrize(
    "bad",
    [
        _token_account("OtherMint" + "1" * 30, "1"),
        _token_account(_SOL_USDC, "1", decimals=9),
        _token_account(_SOL_USDC, "-5"),
        {"pubkey": "x", "account": {"data": "not-parsed"}},
    ],
)
async def test_solana_token_response_mismatch_is_rejected(
    bad: dict[str, object],
) -> None:
    async with httpx2.AsyncClient(transport=_transport(_sol_handlers([bad]))) as client:
        rpc = LocalWalletRpc(client, 8899)
        with pytest.raises(WalletUnavailable, match="token account"):
            await rpc.solana_token_balance(_SOL_ADDR, _SOL_USDC)


@pytest.mark.anyio
async def test_solana_unlisted_mint_and_devnet_token_reads_refused() -> None:
    async with httpx2.AsyncClient(transport=_transport(_sol_handlers([]))) as client:
        rpc = LocalWalletRpc(client, 8899)
        with pytest.raises(ValueError, match="allowlist"):
            await rpc.solana_token_balance(_SOL_ADDR, "Bad" + "1" * 40)
        with pytest.raises(WalletUnavailable):  # devnet genesis != fake mainnet node
            await rpc.solana_token_balance(_SOL_ADDR, _SOL_USDC, network="devnet")


# --- UTXO transaction status -------------------------------------------------------


def _utxo_handlers(
    network_key: str,
    *,
    raw: object,
    header: object | None = None,
) -> dict[str, Handler]:
    network = UTXO_NETWORKS[network_key]
    return {
        "getblockchaininfo": lambda _: {
            "chain": network.chain,
            "initialblockdownload": False,
        },
        "getblockhash": lambda _: network.genesis,
        "getrawtransaction": lambda _: raw,
        "getblockheader": lambda _: header,
        "getblockcount": lambda _: 4242,
    }


async def _utxo_status(
    key: str, handlers: dict[str, Handler], txid: str = _TXID
) -> object:
    network = UTXO_NETWORKS[key]
    async with httpx2.AsyncClient(
        transport=_transport(handlers, version=network.rpc_version)
    ) as client:
        return await LocalWalletRpc(client, network.port).utxo_transaction(
            txid, network
        )


@pytest.mark.anyio
@pytest.mark.parametrize("key", ["btc-balance", "ltc-balance", "dgb-balance"])
@pytest.mark.parametrize(
    ("confirmations", "finality"),
    [(1, "confirmed"), (5, "confirmed"), (6, "finalized")],
)
async def test_utxo_confirmed_and_finalized_states(
    key: str, confirmations: int, finality: str
) -> None:
    handlers = _utxo_handlers(
        key,
        raw={"txid": _TXID, "confirmations": confirmations, "blockhash": _BLOCK_HASH},
        header={"hash": _BLOCK_HASH, "height": 900, "confirmations": confirmations},
    )
    observed = await _utxo_status(key, handlers)
    assert observed.finality == finality  # type: ignore[attr-defined]
    assert observed.confirmations == confirmations  # type: ignore[attr-defined]
    assert observed.block_number == 900 and observed.successful is True  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_utxo_mempool_unknown_and_reorged_states() -> None:
    key = "btc-balance"
    mempool = await _utxo_status(key, _utxo_handlers(key, raw={"txid": _TXID}))
    assert mempool.finality == "processed" and mempool.confirmations == 0  # type: ignore[attr-defined]

    not_found = _utxo_handlers(key, raw=_RpcError(-5))
    unknown = await _utxo_status(key, not_found)
    assert unknown.finality == "unknown"  # type: ignore[attr-defined]

    reorged = _utxo_handlers(
        key,
        raw={"txid": _TXID, "confirmations": 3, "blockhash": _BLOCK_HASH},
        header={"hash": _BLOCK_HASH, "height": 900, "confirmations": -1},
    )
    orphan = await _utxo_status(key, reorged)
    assert orphan.finality == "unknown" and orphan.confirmations == 0  # type: ignore[attr-defined]
    assert orphan.block_hash == _BLOCK_HASH  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_utxo_status_rejects_mismatched_or_malformed_responses() -> None:
    key = "btc-balance"
    other = "ef" * 32
    cases = [
        _utxo_handlers(key, raw={"txid": other}),
        _utxo_handlers(key, raw={"txid": _TXID, "confirmations": 2}),
        _utxo_handlers(key, raw={"txid": _TXID, "blockhash": "zz"}),
        _utxo_handlers(
            key,
            raw={"txid": _TXID, "confirmations": 1, "blockhash": _BLOCK_HASH},
            header={"hash": other, "height": 1, "confirmations": 1},
        ),
        _utxo_handlers(
            key,
            raw={"txid": _TXID, "confirmations": 1, "blockhash": _BLOCK_HASH},
            header={"hash": _BLOCK_HASH},
        ),
        _utxo_handlers(key, raw=_RpcError(-8)),  # any other error code is not "unknown"
    ]
    for handlers in cases:
        with pytest.raises(WalletUnavailable):
            await _utxo_status(key, handlers)
    with pytest.raises(ValueError):
        await _utxo_status(key, cases[0], txid="nothex")


@pytest.mark.anyio
async def test_utxo_status_requires_correct_chain() -> None:
    handlers = _utxo_handlers("btc-balance", raw={"txid": _TXID})
    handlers["getblockhash"] = lambda _: "00" * 32
    with pytest.raises(WalletUnavailable, match="genesis"):
        await _utxo_status("btc-balance", handlers)


@pytest.mark.anyio
@pytest.mark.parametrize("key", ["btc-balance", "ltc-balance"])
async def test_utxo_wallet_adapter_status_and_health(key: str) -> None:
    network = UTXO_NETWORKS[key]
    handlers = _utxo_handlers(key, raw=_RpcError(-5))
    address = (
        _BTC_ADDR
        if key == "btc-balance"
        else "L" + "a" * 26  # shape-only: the checksum path is covered elsewhere
    )
    async with httpx2.AsyncClient(
        transport=_transport(handlers, version=network.rpc_version)
    ) as client:
        rpc = LocalWalletRpc(client, network.port)
        if key == "ltc-balance":
            with pytest.raises(ValueError):  # offline checksum validation applies
                UtxoChainWallet(rpc, key, [address])
            return
        wallet = UtxoChainWallet(rpc, key, [address])
        assert await wallet.get_transaction(_TXID) is None
        health = await wallet.health()
    assert health.healthy and health.height == 4242
    assert wallet.network == network.asset.network.value


@pytest.mark.anyio
async def test_utxo_wallet_health_reports_wrong_chain() -> None:
    handlers = _utxo_handlers("btc-balance", raw=None)
    handlers["getblockchaininfo"] = lambda _: {
        "chain": "test",
        "initialblockdownload": False,
    }
    async with httpx2.AsyncClient(transport=_transport(handlers)) as client:
        wallet = UtxoChainWallet(
            LocalWalletRpc(client, 8332), "btc-balance", [_BTC_ADDR]
        )
        health = await wallet.health()
    assert not health.healthy and "synced mainnet" in health.detail
