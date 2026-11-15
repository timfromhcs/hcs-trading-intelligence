"""Public-address balance reads from operator-managed loopback nodes."""

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

import httpx2
from pydantic import BaseModel, Field

from hcs_trader.domain.assets import AssetId
from hcs_trader.domain.wallet import SolanaBalance, WalletBalance

ETH = AssetId.parse("eip155:1/native:ETH")
BTC = AssetId.parse("bitcoin:mainnet/native:BTC")
LTC = AssetId.parse("litecoin:mainnet/native:LTC")
DGB = AssetId.parse("digibyte:mainnet/native:DGB")
SOL_GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
SOL = AssetId.parse("solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/native:SOL")
_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_HEX_QUANTITY = re.compile(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]{0,63})")
_HASH = re.compile(r"0x[0-9a-fA-F]{64}")
_ADDRESS = re.compile(r"0x[0-9a-f]{40}")
_UTXO_ADDRESS = re.compile(r"[A-Za-z0-9]{14,90}")
_UTXO_HASH = re.compile(r"[0-9a-fA-F]{64}")


class WalletUnavailable(Exception):
    """The read-only node request could not be verified."""


class _RpcResponse(BaseModel):
    jsonrpc: Literal["1.0", "2.0"] | None = None
    id: str
    result: object
    error: object | None = None


class _FinalizedBlock(BaseModel):
    number: str
    hash: str


class _SlotContext(BaseModel):
    slot: int = Field(ge=0, le=2**64 - 1)


class _LamportBalance(BaseModel):
    context: _SlotContext
    value: int = Field(ge=0, le=2**64 - 1)


class _UtxoChain(BaseModel):
    chain: str
    initialblockdownload: bool


class _ValidatedAddress(BaseModel):
    isvalid: bool


class _UtxoScan(BaseModel):
    success: Literal[True]
    height: int = Field(ge=0)
    bestblock: str
    total_amount: Decimal | int


@dataclass(frozen=True, slots=True)
class UtxoNetwork:
    name: str
    asset: AssetId
    genesis: str
    port: int
    rpc_version: Literal["1.0", "2.0"]
    max_coins: int


UTXO_NETWORKS = {
    "btc-balance": UtxoNetwork(
        "Bitcoin",
        BTC,
        "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f",
        8332,
        "2.0",
        21_000_000,
    ),
    "ltc-balance": UtxoNetwork(
        "Litecoin",
        LTC,
        "12a765e31ffd4059bada1e25190f6e98c99d9714d334efa41a195a7e7e04bfe2",
        9332,
        "1.0",
        84_000_000,
    ),
    "dgb-balance": UtxoNetwork(
        "DigiByte",
        DGB,
        "7497ea1b465eb39f1c8f507bc877078fe016d6fcb6dfad3a64c98dcc6e1e8496",
        14022,
        "1.0",
        21_000_000_000,
    ),
}


class LocalWalletRpc:
    def __init__(self, client: httpx2.AsyncClient, port: int) -> None:
        if not 1 <= port <= 65535:
            raise ValueError("Wallet RPC port must be between 1 and 65535")
        self._client = client
        self._url = f"http://127.0.0.1:{port}/"

    async def _request(
        self,
        method: str,
        parameters: list[object],
        *,
        timeout: int = 5,
        version: Literal["1.0", "2.0"] = "2.0",
    ) -> object:
        request_id = uuid4().hex
        try:
            async with (
                asyncio.timeout(timeout),
                self._client.stream(
                    "POST",
                    self._url,
                    json={
                        "jsonrpc": version,
                        "id": request_id,
                        "method": method,
                        "params": parameters,
                    },
                    follow_redirects=False,
                    timeout=timeout,
                ) as response,
            ):
                if response.status_code != 200:
                    raise WalletUnavailable("Local wallet node request failed")
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(payload) + len(chunk) > 524_288:
                        raise WalletUnavailable(
                            "Wallet node response exceeds the size limit"
                        )
                    payload.extend(chunk)
            # Some UTXO nodes return coin amounts as JSON numbers.
            envelope = _RpcResponse.model_validate(
                json.loads(payload, parse_float=Decimal), strict=True
            )
        except (httpx2.HTTPError, TimeoutError, ValueError):
            raise WalletUnavailable(
                "Local wallet node response unavailable or invalid"
            ) from None
        valid_version = envelope.jsonrpc == version or (
            version == "1.0" and envelope.jsonrpc is None
        )
        if not valid_version or envelope.id != request_id or envelope.error is not None:
            raise WalletUnavailable(
                "Local wallet node returned an invalid RPC envelope"
            )
        return envelope.result

    async def ethereum_balance(self, address: str) -> WalletBalance:
        if not _ADDRESS.fullmatch(address):
            raise ValueError("Expected a lowercase 20-byte Ethereum hex address")
        if _quantity(await self._request("eth_chainId", [])) != 1:
            raise WalletUnavailable("Local node is not Ethereum mainnet")
        try:
            block = _FinalizedBlock.model_validate(
                await self._request("eth_getBlockByNumber", ["finalized", False]),
                strict=True,
            )
        except ValueError:
            raise WalletUnavailable(
                "Local node did not return a finalized block"
            ) from None
        height = _quantity(block.number)
        if not _HASH.fullmatch(block.hash):
            raise WalletUnavailable("Invalid finalized block hash")
        units = _quantity(
            await self._request(
                "eth_getBalance",
                [address, {"blockHash": block.hash, "requireCanonical": True}],
            )
        )
        return WalletBalance(
            address, str(ETH), str(units), 18, height, block.hash, datetime.now(UTC)
        )

    async def solana_balance(self, address: str) -> SolanaBalance:
        if not _is_solana_address(address):
            raise ValueError("Expected a 32-byte Solana base58 address")
        if await self._request("getGenesisHash", []) != SOL_GENESIS:
            raise WalletUnavailable("Local node is not Solana mainnet-beta")
        try:
            result = _LamportBalance.model_validate(
                await self._request(
                    "getBalance", [address, {"commitment": "finalized"}]
                ),
                strict=True,
            )
        except ValueError:
            raise WalletUnavailable("Invalid finalized Solana balance") from None
        return SolanaBalance(
            address,
            str(SOL),
            str(result.value),
            9,
            result.context.slot,
            SOL_GENESIS,
            datetime.now(UTC),
        )

    async def bitcoin_balance(self, address: str) -> WalletBalance:
        return await self.utxo_balance(address, UTXO_NETWORKS["btc-balance"])

    async def utxo_balance(self, address: str, network: UtxoNetwork) -> WalletBalance:
        if not _UTXO_ADDRESS.fullmatch(address):
            raise ValueError(f"Expected a {network.name} address")
        try:
            chain = _UtxoChain.model_validate(
                await self._request(
                    "getblockchaininfo", [], version=network.rpc_version
                ),
                strict=True,
            )
            if chain.chain != "main" or chain.initialblockdownload:
                raise WalletUnavailable(
                    f"{network.name} node must report synced mainnet"
                )
            genesis = await self._request(
                "getblockhash", [0], version=network.rpc_version
            )
            if genesis != network.genesis:
                raise WalletUnavailable(
                    f"Local node does not match the {network.name} genesis"
                )
            validated = _ValidatedAddress.model_validate(
                await self._request(
                    "validateaddress", [address], version=network.rpc_version
                ),
                strict=True,
            )
            if not validated.isvalid:
                raise WalletUnavailable(
                    f"Address is invalid for {network.name} mainnet"
                )
            scan = _UtxoScan.model_validate(
                await self._request(
                    "scantxoutset",
                    ["start", [f"addr({address})"]],
                    timeout=180,
                    version=network.rpc_version,
                ),
                strict=True,
            )
        except ValueError:
            raise WalletUnavailable(
                f"Invalid {network.name} node balance response"
            ) from None

        amount = Decimal(scan.total_amount)
        if (
            not _UTXO_HASH.fullmatch(scan.bestblock)
            or not amount.is_finite()
            or not 0 <= amount <= network.max_coins
            or int(amount.as_tuple().exponent) < -8
        ):
            raise WalletUnavailable(f"Invalid {network.name} scan block or amount")
        numerator, denominator = amount.as_integer_ratio()
        return WalletBalance(
            address,
            str(network.asset),
            str(numerator * 100_000_000 // denominator),
            8,
            scan.height,
            scan.bestblock,
            datetime.now(UTC),
        )


def _quantity(value: object) -> int:
    if not isinstance(value, str) or not _HEX_QUANTITY.fullmatch(value):
        raise WalletUnavailable("Invalid Ethereum RPC quantity")
    return int(value, 16)


def _is_solana_address(address: str) -> bool:
    if not 32 <= len(address) <= 44 or any(char not in _BASE58 for char in address):
        return False
    decoded = 0
    for char in address:
        decoded = decoded * 58 + _BASE58.index(char)
    leading_zero_bytes = len(address) - len(address.lstrip("1"))
    return leading_zero_bytes + (decoded.bit_length() + 7) // 8 == 32
