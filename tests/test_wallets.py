import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from hcs_trader import __main__ as launcher
from hcs_trader.api import create_app
from hcs_trader.domain.wallet import SolanaBalance, WalletBalance
from hcs_trader.storage import AuditStore
from hcs_trader.wallets import (
    BITCOIN_NETWORKS,
    ETH,
    SEPOLIA_ETH,
    SOL,
    SOL_DEVNET,
    SOL_DEVNET_GENESIS,
    SOL_GENESIS,
    UTXO_NETWORK_PROFILES,
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
@pytest.mark.parametrize("network", ["mainnet", "sepolia"])
async def test_eth_balance_pins_finalized_block_and_preserves_wei(network: str) -> None:
    calls: list[str] = []
    wei = 10**20 + 1

    def node(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == "http://127.0.0.1:8545/"
        assert request.method == "POST"
        body = json.loads(request.content)
        method = body["method"]
        calls.append(method)
        if method == "eth_chainId":
            result: object = "0x1" if network == "mainnet" else hex(11155111)
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
        rpc = LocalWalletRpc(client, 8545)
        balance = await rpc.ethereum_balance(_ADDRESS, network=network)
        other = "sepolia" if network == "mainnet" else "mainnet"
        with pytest.raises(WalletUnavailable, match=other):
            await rpc.ethereum_balance(_ADDRESS, network=other)
    assert calls == [
        "eth_chainId",
        "eth_getBlockByNumber",
        "eth_getBalance",
        "eth_chainId",
    ]
    assert balance.asset == str(ETH if network == "mainnet" else SEPOLIA_ETH)
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
@pytest.mark.parametrize("network", ["mainnet-beta", "devnet"])
async def test_solana_checks_genesis_and_preserves_finalized_lamports(
    network: str,
) -> None:
    address = "1" * 31 + "2"
    calls = []
    genesis = SOL_DEVNET_GENESIS if network == "devnet" else SOL_GENESIS

    def node(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == "http://127.0.0.1:8899/"
        body = json.loads(request.content)
        calls.append(body["method"])
        if body["method"] == "getGenesisHash":
            result: object = genesis
        else:
            assert body["method"] == "getBalance"
            assert body["params"] == [address, {"commitment": "finalized"}]
            result = {"context": {"slot": 120}, "value": 2**64 - 1}
        return httpx2.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(node)) as client:
        rpc = LocalWalletRpc(client, 8899)
        balance = await rpc.solana_balance(address, network=network)
        other = "mainnet-beta" if network == "devnet" else "devnet"
        with pytest.raises(WalletUnavailable, match=other):
            await rpc.solana_balance(address, network=other)
    assert calls == ["getGenesisHash", "getBalance", "getGenesisHash"]
    assert balance.asset == str(SOL_DEVNET if network == "devnet" else SOL)
    assert balance.genesis_hash == genesis
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
@pytest.mark.parametrize(
    "command, profile",
    [
        (command, profile)
        for command, profiles in UTXO_NETWORK_PROFILES.items()
        for profile in profiles
    ],
)
async def test_utxo_uses_a_single_address_descriptor_and_preserves_base_units(
    command: str,
    profile: str,
) -> None:
    network = UTXO_NETWORK_PROFILES[command][profile]
    address = "1" + "a" * 25
    calls = []

    def node(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == f"http://127.0.0.1:{network.port}/"
        body = json.loads(request.content)
        assert body["jsonrpc"] == network.rpc_version
        calls.append(body["method"])
        if body["method"] == "getblockchaininfo":
            result = json.dumps(
                {
                    "chain": network.chain,
                    "initialblockdownload": False,
                    "signet_challenge": network.signet_challenge,
                }
            )
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
@pytest.mark.parametrize("challenge", [None, "51"])
async def test_bitcoin_rejects_custom_or_unidentified_signet(
    challenge: str | None,
) -> None:
    def node(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        assert body["method"] == "getblockchaininfo"
        return httpx2.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "chain": "signet",
                    "initialblockdownload": False,
                    "signet_challenge": challenge,
                },
            },
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(node)) as client:
        with pytest.raises(WalletUnavailable, match="default Signet challenge"):
            await LocalWalletRpc(client, 38332).utxo_balance(
                "1" + "a" * 25, BITCOIN_NETWORKS["signet"]
            )


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


def test_snapshot_save_is_opt_in_and_offline_api_keeps_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "wallet.sqlite"
    now = datetime.now(UTC)
    ethereum = WalletBalance(_ADDRESS, str(ETH), "123", 18, 35, _BLOCK_HASH, now)

    async def read_fixture(address: str, port: int, network: str) -> WalletBalance:
        assert address == _ADDRESS and port == 8545
        return (
            ethereum
            if network == "mainnet"
            else replace(ethereum, asset=str(SEPOLIA_ETH))
        )

    monkeypatch.setattr(launcher, "ethereum_balance", read_fixture)
    monkeypatch.setattr(launcher, "local_store", lambda: AuditStore(path))
    monkeypatch.setattr("sys.argv", ["hcs-trader", "eth-balance", _ADDRESS])
    launcher.main()
    assert not path.exists()
    assert json.loads(capsys.readouterr().out)["base_units"] == "123"
    monkeypatch.setattr("sys.argv", ["hcs-trader", "eth-balance", _ADDRESS, "--save"])
    launcher.main()

    store = AuditStore(path)
    monkeypatch.setattr(
        "sys.argv",
        ["hcs-trader", "eth-balance", _ADDRESS, "--network", "sepolia", "--save"],
    )
    launcher.main()
    (sepolia,) = AuditStore(path.with_name("wallet-sepolia.sqlite")).wallet_snapshots(
        limit=10
    )
    assert sepolia.asset == str(SEPOLIA_ETH)
    store.save_wallet_snapshot(
        replace(ethereum, observed_at=now - timedelta(seconds=1), base_units="456")
    )
    solana = SolanaBalance(
        "1" * 32, str(SOL), "789", 9, 100, SOL_GENESIS, now + timedelta(seconds=1)
    )
    store.save_wallet_snapshot(solana)
    with TestClient(create_app(AuditStore(path)), base_url="http://127.0.0.1") as api:
        result = api.get("/api/v1/wallets")
        assert result.headers["cache-control"] == "no-store"
        recent, prior = result.json()
        assert recent["asset"] == str(SOL) and recent["slot"] == 100
        assert prior["asset"] == str(ETH) and prior["base_units"] == "123"
        assert api.get("/api/v1/wallets?limit=1").json() == [recent]
        assert api.get("/api/v1/wallets?limit=101").status_code == 422


@pytest.mark.anyio
@pytest.mark.parametrize("canonical", [True, False])
@pytest.mark.parametrize("network", ["mainnet", "sepolia"])
async def test_ethereum_receipt_requires_canonical_block(
    canonical: bool, network: str
) -> None:
    txid = "0x" + "b1" * 32

    def node(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        match body["method"]:
            case "eth_chainId":
                result: object = "0x1" if network == "mainnet" else hex(11155111)
            case "eth_getTransactionReceipt":
                assert body["params"] == [txid]
                result = {
                    "transactionHash": txid,
                    "blockNumber": "0x1",
                    "blockHash": _BLOCK_HASH,
                    "status": "0x0",
                }
            case "eth_getBlockByNumber":
                if body["params"][0] == "finalized":
                    result = {"number": "0x2", "hash": "0x" + "ab" * 32}
                else:
                    assert body["params"] == ["0x1", False]
                    result = {
                        "number": "0x1",
                        "hash": _BLOCK_HASH if canonical else "0x" + "aa" * 32,
                    }
            case _:
                raise AssertionError("Unexpected RPC")
        return httpx2.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(node)) as client:
        rpc = LocalWalletRpc(client, 8545)
        if not canonical:
            with pytest.raises(WalletUnavailable, match="canonical"):
                await rpc.ethereum_transaction(txid, network=network)
        else:
            observed = await rpc.ethereum_transaction(txid, network=network)
            assert (
                observed.network
                == (ETH if network == "mainnet" else SEPOLIA_ETH).network.value
            )
            assert observed.finality == "finalized" and observed.successful is False
            assert observed.block_number == 1 and observed.block_hash == _BLOCK_HASH


@pytest.mark.anyio
@pytest.mark.parametrize("present", [True, False])
@pytest.mark.parametrize("network", ["mainnet-beta", "devnet"])
async def test_solana_status_preserves_confirmation_or_unknown(
    present: bool, network: str
) -> None:
    signature = "1" * 63 + "2"

    def node(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if body["method"] == "getGenesisHash":
            result: object = SOL_DEVNET_GENESIS if network == "devnet" else SOL_GENESIS
        else:
            assert body["method"] == "getSignatureStatuses"
            assert body["params"] == [[signature], {"searchTransactionHistory": True}]
            state = {"slot": 12, "confirmationStatus": "confirmed", "err": None}
            result = {"context": {"slot": 15}, "value": [state if present else None]}
        return httpx2.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(node)) as client:
        observed = await LocalWalletRpc(client, 8899).solana_transaction(
            signature, network=network
        )
    assert (
        observed.network == (SOL_DEVNET if network == "devnet" else SOL).network.value
    )
    assert observed.finality == ("confirmed" if present else "unknown")
    assert observed.successful is (True if present else None)
    assert observed.slot == (12 if present else None)
