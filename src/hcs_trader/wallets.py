"""Public-address balance reads from operator-managed loopback nodes."""

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

import httpx2
from pydantic import BaseModel

from hcs_trader.domain.assets import AssetId

ETH = AssetId.parse("eip155:1/native:ETH")
_HEX_QUANTITY = re.compile(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]{0,63})")
_HASH = re.compile(r"0x[0-9a-fA-F]{64}")
_ADDRESS = re.compile(r"0x[0-9a-f]{40}")


@dataclass(frozen=True, slots=True)
class WalletBalance:
    address: str
    asset: str
    base_units: str
    decimals: int
    block_number: int
    block_hash: str
    observed_at: datetime


class WalletUnavailable(Exception):
    """The read-only node request could not be verified."""


class _RpcResponse(BaseModel):
    jsonrpc: Literal["2.0"]
    id: str
    result: object
    error: object | None = None


class _FinalizedBlock(BaseModel):
    number: str
    hash: str


class LocalWalletRpc:
    def __init__(self, client: httpx2.AsyncClient, port: int) -> None:
        if not 1 <= port <= 65535:
            raise ValueError("Wallet RPC port must be between 1 and 65535")
        self._client = client
        self._url = f"http://127.0.0.1:{port}/"

    async def _request(self, method: str, parameters: list[object]) -> object:
        request_id = uuid4().hex
        try:
            async with (
                asyncio.timeout(5),
                self._client.stream(
                    "POST",
                    self._url,
                    json={
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": method,
                        "params": parameters,
                    },
                    follow_redirects=False,
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
            envelope = _RpcResponse.model_validate_json(payload, strict=True)
        except (httpx2.HTTPError, TimeoutError, ValueError):
            raise WalletUnavailable(
                "Local wallet node response unavailable or invalid"
            ) from None
        if envelope.id != request_id or envelope.error is not None:
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


def _quantity(value: object) -> int:
    if not isinstance(value, str) or not _HEX_QUANTITY.fullmatch(value):
        raise WalletUnavailable("Invalid Ethereum RPC quantity")
    return int(value, 16)
