import json

import httpx2
import pytest

from hcs_trader.wallets import ETH, LocalWalletRpc, WalletUnavailable

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
