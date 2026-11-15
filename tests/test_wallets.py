import json

import httpx2
import pytest

from hcs_trader.wallets import (
    ETH,
    SOL,
    SOL_GENESIS,
    UTXO_NETWORKS,
    LocalWalletRpc,
    WalletUnavailable,
)

_ADDRESS = "0x" + "12" * 20
_BLOCK_HASH = "0x" + "34" * 32


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_eth_balance_pins_finalized_block_and_preserves_wei() -> None:
    calls: list[str] = []
    wei = 10**20 + 1

    def node(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == "http://127.0.0.1:8545/"
        assert request.method == "POST"
        body = json.loads(request.content)
        method = body["method"]
        calls.append(method)
        if method == "eth_chainId":
            result: object = "0x1"
        elif method == "eth_getBlockByNumber":
            assert body["params"] == ["finalized", False]
            result = {"number": "0x100", "hash": _BLOCK_HASH}
        else:
            assert method == "eth_getBalance"
            assert body["params"] == [
                _ADDRESS,
                {"blockHash": _BLOCK_HASH, "requireCanonical": True},
            ]
            result = hex(wei)
        return httpx2.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(node)) as client:
        balance = await LocalWalletRpc(client, 8545).ethereum_balance(_ADDRESS)
    assert calls == ["eth_chainId", "eth_getBlockByNumber", "eth_getBalance"]
    assert balance.asset == str(ETH)
    assert balance.base_units == str(wei)
    assert balance.decimals == 18
    assert balance.block_number == 256
    assert balance.block_hash == _BLOCK_HASH


@pytest.mark.anyio
@pytest.mark.parametrize(
    "chain, mismatched_id", [("0x2", False), ("0x1", True), ("0x01", False)]
)
async def test_eth_balance_stops_on_unverifiable_node(
    chain: str, mismatched_id: bool
) -> None:
    def node(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        assert body["method"] == "eth_chainId"
        return httpx2.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "unexpected" if mismatched_id else body["id"],
                "result": chain,
            },
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(node)) as client:
        with pytest.raises(WalletUnavailable):
            await LocalWalletRpc(client, 8545).ethereum_balance(_ADDRESS)
        with pytest.raises(ValueError, match="20-byte"):
            await LocalWalletRpc(client, 8545).ethereum_balance("invalid")


@pytest.mark.anyio
async def test_solana_checks_genesis_and_preserves_finalized_lamports() -> None:
    address = "1" * 31 + "2"
    calls = []

    def node(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == "http://127.0.0.1:8899/"
        body = json.loads(request.content)
        calls.append(body["method"])
        if body["method"] == "getGenesisHash":
            result: object = SOL_GENESIS
        else:
            assert body["method"] == "getBalance"
            assert body["params"] == [address, {"commitment": "finalized"}]
            result = {"context": {"slot": 120}, "value": 2**64 - 1}
        return httpx2.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(node)) as client:
        balance = await LocalWalletRpc(client, 8899).solana_balance(address)
    assert calls == ["getGenesisHash", "getBalance"]
    assert balance.asset == str(SOL)
    assert balance.base_units == str(2**64 - 1)
    assert balance.slot == 120 and balance.decimals == 9


@pytest.mark.anyio
async def test_solana_rejects_bad_addresses_and_wrong_cluster() -> None:
    def node(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        assert body["method"] == "getGenesisHash"
        return httpx2.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": "wrong-cluster"}
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(node)) as client:
        rpc = LocalWalletRpc(client, 8899)
        with pytest.raises(ValueError, match="32-byte"):
            await rpc.solana_balance("2" * 32)
        with pytest.raises(ValueError, match="32-byte"):
            await rpc.solana_balance("0" * 32)
        with pytest.raises(WalletUnavailable, match="mainnet-beta"):
            await rpc.solana_balance("1" * 32)


@pytest.mark.anyio
@pytest.mark.parametrize("command", ["btc-balance", "ltc-balance", "dgb-balance"])
async def test_utxo_uses_a_single_address_descriptor_and_preserves_base_units(
    command: str,
) -> None:
    network = UTXO_NETWORKS[command]
    address = "1" + "a" * 25
    calls = []

    def node(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == f"http://127.0.0.1:{network.port}/"
        body = json.loads(request.content)
        assert body["jsonrpc"] == network.rpc_version
        calls.append(body["method"])
        if body["method"] == "getblockchaininfo":
            result = '{"chain":"main","initialblockdownload":false}'
        elif body["method"] == "getblockhash":
            assert body["params"] == [0]
            result = json.dumps(network.genesis)
        elif body["method"] == "validateaddress":
            assert body["params"] == [address]
            result = '{"isvalid":true}'
        else:
            assert body["method"] == "scantxoutset"
            assert body["params"] == ["start", [f"addr({address})"]]
            block = "4" * 64
            result = (
                '{"success":true,"height":1000,'
                f'"bestblock":"{block}","total_amount":12345678.12345679}}'
            )
        version = (
            '"jsonrpc":"2.0",' if network.rpc_version == "2.0" else '"error":null,'
        )
        return httpx2.Response(
            200, content=f'{{{version}"id":"{body["id"]}","result":{result}}}'
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(node)) as client:
        rpc = LocalWalletRpc(client, network.port)
        with pytest.raises(ValueError):
            await rpc.utxo_balance("1abc)#raw(00)", network)
        assert calls == []
        balance = await rpc.utxo_balance(address, network)
    assert calls == [
        "getblockchaininfo",
        "getblockhash",
        "validateaddress",
        "scantxoutset",
    ]
    assert balance.asset == str(network.asset)
    assert balance.base_units == "1234567812345679"
    assert balance.block_number == 1000 and balance.decimals == 8


@pytest.mark.anyio
@pytest.mark.parametrize(("network", "syncing"), [("regtest", False), ("main", True)])
async def test_bitcoin_rejects_wrong_network_or_syncing_node(
    network: str, syncing: bool
) -> None:
    def node(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        assert body["method"] == "getblockchaininfo"
        return httpx2.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"chain": network, "initialblockdownload": syncing},
            },
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(node)) as client:
        with pytest.raises(WalletUnavailable, match="synced mainnet"):
            await LocalWalletRpc(client, 8332).bitcoin_balance("1" + "a" * 25)


@pytest.mark.anyio
async def test_litecoin_rejects_bitcoin_even_if_node_reports_mainnet() -> None:
    def node(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if body["method"] == "getblockchaininfo":
            result: object = {"chain": "main", "initialblockdownload": False}
        else:
            assert body["method"] == "getblockhash"
            result = UTXO_NETWORKS["btc-balance"].genesis
        return httpx2.Response(
            200, json={"id": body["id"], "result": result, "error": None}
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(node)) as client:
        with pytest.raises(WalletUnavailable, match="Litecoin genesis"):
            await LocalWalletRpc(client, 9332).utxo_balance(
                "1" + "a" * 25, UTXO_NETWORKS["ltc-balance"]
            )
