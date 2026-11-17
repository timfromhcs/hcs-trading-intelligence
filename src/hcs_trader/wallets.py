"""Public-address balance reads from operator-managed loopback nodes."""

import asyncio
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

import httpx2
from pydantic import BaseModel, Field

from hcs_trader.domain.assets import AssetId
from hcs_trader.domain.wallet import (
    SolanaBalance,
    TransactionObservation,
    WalletBalance,
)
from hcs_trader.domain.wallet_port import (
    BroadcastResult,
    ChainBalance,
    ChainHealth,
    SignedTransaction,
    SimulationResult,
    TransferOrSwapIntent,
    UnsignedTransaction,
    WalletAddress,
    WalletOperationUnsupported,
    WalletRole,
)
from hcs_trader.evm_swap import (
    SELECTOR_BALANCE_OF,
    SELECTOR_DECIMALS,
    allowlist_for_chain,
)
from hcs_trader.jupiter_swap import ALLOWED_SOLANA_MINTS
from hcs_trader.wallet_addresses import (
    has_offline_profile,
    validate_evm_address,
    validate_utxo_address,
)

ETH = AssetId.parse("eip155:1/native:ETH")
SEPOLIA_ETH = AssetId.parse("eip155:11155111/native:ETH")
ETHEREUM_NETWORKS = {
    "mainnet": (1, ETH),
    "sepolia": (11155111, SEPOLIA_ETH),
}
BTC = AssetId.parse("bitcoin:mainnet/native:BTC")
LTC = AssetId.parse("litecoin:mainnet/native:LTC")
DGB = AssetId.parse("digibyte:mainnet/native:DGB")
SOL_GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
SOL_DEVNET_GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"
SOL = AssetId.parse("solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/native:SOL")
SOL_DEVNET = AssetId.parse(f"solana:{SOL_DEVNET_GENESIS[:32]}/native:SOL")
SOLANA_NETWORKS = {
    "mainnet-beta": (SOL_GENESIS, SOL),
    "devnet": (SOL_DEVNET_GENESIS, SOL_DEVNET),
}
_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_HEX_QUANTITY = re.compile(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]{0,63})")
_HASH = re.compile(r"0x[0-9a-fA-F]{64}")
_ADDRESS = re.compile(r"0x[0-9a-f]{40}")
_UTXO_ADDRESS = re.compile(r"[A-Za-z0-9]{14,90}")
_UTXO_HASH = re.compile(r"[0-9a-fA-F]{64}")
_WALLET_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


class WalletUnavailable(Exception):
    """The read-only node request could not be verified."""


class _RpcResponse(BaseModel):
    jsonrpc: Literal["1.0", "2.0"] | None = None
    id: str
    result: object = None
    error: object | None = None


_NOT_FOUND = object()
# Bitcoin-family RPC_INVALID_ADDRESS_OR_KEY: no such mempool/blockchain tx.
_RPC_NOT_FOUND_CODE = -5
UTXO_FINAL_CONFIRMATIONS = 6
SOLANA_TOKEN_DECIMALS = {
    "So11111111111111111111111111111111111111112": 9,
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": 6,
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": 6,
}


class _FinalizedBlock(BaseModel):
    number: str
    hash: str


class _EthReceipt(BaseModel):
    transaction_hash: str = Field(alias="transactionHash")
    block_number: str = Field(alias="blockNumber")
    block_hash: str = Field(alias="blockHash")
    status: Literal["0x0", "0x1"] | None = None


class _SlotContext(BaseModel):
    slot: int = Field(ge=0, le=2**64 - 1)


class _LamportBalance(BaseModel):
    context: _SlotContext
    value: int = Field(ge=0, le=2**64 - 1)


class _SignatureStatus(BaseModel):
    slot: int = Field(ge=0, le=2**64 - 1)
    finality: Literal["processed", "confirmed", "finalized"] = Field(
        alias="confirmationStatus"
    )
    err: object


class _SignatureStatuses(BaseModel):
    context: _SlotContext
    value: list[_SignatureStatus | None] = Field(min_length=1, max_length=1)


class _UtxoChain(BaseModel):
    chain: str
    initialblockdownload: bool
    signet_challenge: str | None = None


class _ValidatedAddress(BaseModel):
    isvalid: bool


class _UtxoRawTransaction(BaseModel):
    txid: str
    confirmations: int | None = Field(default=None, ge=0)
    blockhash: str | None = None


class _UtxoBlockHeader(BaseModel):
    hash: str
    height: int = Field(ge=0)
    confirmations: int


class _TokenAmount(BaseModel):
    amount: str
    decimals: int = Field(ge=0, le=36)


class _ParsedTokenInfo(BaseModel):
    mint: str
    tokenAmount: _TokenAmount  # noqa: N815 - RPC field name


class _ParsedTokenData(BaseModel):
    info: _ParsedTokenInfo


class _ParsedTokenAccountData(BaseModel):
    parsed: _ParsedTokenData


class _TokenAccountValue(BaseModel):
    account: dict[str, object]


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
    chain: str = "main"
    signet_challenge: str | None = None


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


BITCOIN_NETWORKS = {
    "mainnet": UTXO_NETWORKS["btc-balance"],
    "regtest": UtxoNetwork(
        "Bitcoin",
        AssetId.parse("bitcoin:regtest/native:BTC"),
        "0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206",
        18443,
        "2.0",
        21_000_000,
        chain="regtest",
    ),
    "signet": UtxoNetwork(
        "Bitcoin",
        AssetId.parse("bitcoin:signet/native:BTC"),
        "00000008819873e925422c1ff0f99f7cc9bbb232af63a077a480a3633bee1ef6",
        38332,
        "2.0",
        21_000_000,
        chain="signet",
        signet_challenge=(
            "512103ad5e0edad18cb1f0fc0d28a3d4f1f3e445640337489abb10404f2d1e086be4302103"
            "59ef5021964fe22d6f8e05b2463c9540ce96883fe3b278760f048f5189f2e6c452ae"
        ),
    ),
}

UTXO_NETWORK_PROFILES = {
    "btc-balance": BITCOIN_NETWORKS,
    "ltc-balance": {
        "mainnet": UTXO_NETWORKS["ltc-balance"],
        "regtest": UtxoNetwork(
            "Litecoin",
            AssetId.parse("litecoin:regtest/native:LTC"),
            "530827f38f93b43ed12af0b3ad25a288dc02ed74d6d7857862df51fc56c416f9",
            19443,
            "1.0",
            84_000_000,
            chain="regtest",
        ),
    },
    "dgb-balance": {
        "mainnet": UTXO_NETWORKS["dgb-balance"],
        "regtest": UtxoNetwork(
            "DigiByte",
            AssetId.parse("digibyte:regtest/native:DGB"),
            "4598a0f2b823aaf9e77ee6d5e46f1edb824191dcd48b08437b7cec17e6ae6e26",
            18443,
            "1.0",
            21_000_000_000,
            chain="regtest",
        ),
    },
}


class LocalWalletRpc:
    def __init__(
        self,
        client: httpx2.AsyncClient,
        port: int,
        wallet_name: str | None = None,
    ) -> None:
        if not 1 <= port <= 65535:
            raise ValueError("Wallet RPC port must be between 1 and 65535")
        if wallet_name is not None and not _WALLET_NAME.fullmatch(wallet_name):
            raise ValueError("Wallet name must be 1-64 letters, digits, _ or -")
        self._client = client
        suffix = f"wallet/{wallet_name}" if wallet_name else ""
        self._url = f"http://127.0.0.1:{port}/{suffix}"

    async def call(
        self,
        method: str,
        parameters: list[object],
        *,
        timeout: int = 30,
        version: Literal["1.0", "2.0"] = "2.0",
    ) -> object:
        """Issue one loopback JSON-RPC call (used by the regtest PSBT flow)."""
        return await self._request(method, parameters, timeout=timeout, version=version)

    async def _request(
        self,
        method: str,
        parameters: list[object],
        *,
        timeout: int = 5,
        version: Literal["1.0", "2.0"] = "2.0",
        not_found_ok: bool = False,
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
        if (
            not_found_ok
            and valid_version
            and envelope.id == request_id
            and isinstance(envelope.error, dict)
            and envelope.error.get("code") == _RPC_NOT_FOUND_CODE
        ):
            return _NOT_FOUND
        if envelope.error is None and "result" not in envelope.model_fields_set:
            raise WalletUnavailable(
                "Local wallet node returned an invalid RPC envelope"
            )
        if not valid_version or envelope.id != request_id or envelope.error is not None:
            raise WalletUnavailable(
                "Local wallet node returned an invalid RPC envelope"
            )
        return envelope.result

    async def _ethereum_asset(self, network: str) -> AssetId:
        profile = ETHEREUM_NETWORKS.get(network)
        if profile is None:
            raise ValueError("Unsupported Ethereum network")
        chain_id, asset = profile
        if _quantity(await self._request("eth_chainId", [])) != chain_id:
            raise WalletUnavailable(f"Local node is not Ethereum {network}")
        return asset

    async def ethereum_balance(
        self, address: str, *, network: str = "mainnet"
    ) -> WalletBalance:
        if not _ADDRESS.fullmatch(address):
            raise ValueError("Expected a lowercase 20-byte Ethereum hex address")
        asset = await self._ethereum_asset(network)
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
            address, str(asset), str(units), 18, height, block.hash, datetime.now(UTC)
        )

    async def _solana_identity(self, network: str) -> tuple[str, AssetId]:
        profile = SOLANA_NETWORKS.get(network)
        if profile is None:
            raise ValueError("Unsupported Solana network")
        genesis, asset = profile
        if await self._request("getGenesisHash", []) != genesis:
            raise WalletUnavailable(f"Local node is not Solana {network}")
        return genesis, asset

    async def solana_balance(
        self, address: str, *, network: str = "mainnet-beta"
    ) -> SolanaBalance:
        if not _is_solana_address(address):
            raise ValueError("Expected a 32-byte Solana base58 address")
        genesis, asset = await self._solana_identity(network)
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
            str(asset),
            str(result.value),
            9,
            result.context.slot,
            genesis,
            datetime.now(UTC),
        )

    async def bitcoin_balance(self, address: str) -> WalletBalance:
        return await self.utxo_balance(address, UTXO_NETWORKS["btc-balance"])

    async def ethereum_transaction(
        self, txid: str, *, network: str = "mainnet"
    ) -> TransactionObservation:
        if not _HASH.fullmatch(txid):
            raise ValueError("Expected a 32-byte Ethereum transaction hash")
        txid = txid.lower()
        asset = await self._ethereum_asset(network)
        payload = await self._request("eth_getTransactionReceipt", [txid])
        if payload is None:
            return TransactionObservation(
                asset.network.value, txid, datetime.now(UTC), "unknown"
            )
        try:
            receipt = _EthReceipt.model_validate(payload, strict=True)
            height = _quantity(receipt.block_number)
            if receipt.transaction_hash.lower() != txid or not _HASH.fullmatch(
                receipt.block_hash
            ):
                raise WalletUnavailable(
                    "Ethereum receipt does not match the requested transaction"
                )
            canonical = _FinalizedBlock.model_validate(
                await self._request(
                    "eth_getBlockByNumber", [receipt.block_number, False]
                ),
                strict=True,
            )
            finalized = _FinalizedBlock.model_validate(
                await self._request("eth_getBlockByNumber", ["finalized", False]),
                strict=True,
            )
        except ValueError:
            raise WalletUnavailable("Invalid Ethereum receipt or block") from None
        if (
            _quantity(canonical.number) != height
            or canonical.hash.lower() != receipt.block_hash.lower()
            or not _HASH.fullmatch(finalized.hash)
        ):
            raise WalletUnavailable(
                "Receipt does not match the canonical Ethereum block"
            )
        return TransactionObservation(
            asset.network.value,
            txid,
            datetime.now(UTC),
            "finalized" if height <= _quantity(finalized.number) else "confirmed",
            receipt.status == "0x1" if receipt.status is not None else None,
            block_number=height,
            block_hash=canonical.hash,
        )

    async def solana_transaction(
        self, signature: str, *, network: str = "mainnet-beta"
    ) -> TransactionObservation:
        if not _is_base58_size(signature, 64):
            raise ValueError("Expected a 64-byte Solana transaction signature")
        _, asset = await self._solana_identity(network)
        try:
            statuses = _SignatureStatuses.model_validate(
                await self._request(
                    "getSignatureStatuses",
                    [[signature], {"searchTransactionHistory": True}],
                ),
                strict=True,
            )
        except ValueError:
            raise WalletUnavailable("Invalid Solana signature status") from None
        status = statuses.value[0]
        if status is None:
            return TransactionObservation(
                asset.network.value, signature, datetime.now(UTC), "unknown"
            )
        if status.slot > statuses.context.slot:
            raise WalletUnavailable("Solana transaction slot exceeds the node context")
        return TransactionObservation(
            asset.network.value,
            signature,
            datetime.now(UTC),
            status.finality,
            status.err is None,
            slot=status.slot,
        )

    async def ethereum_token_balance(
        self, address: str, token: str, *, network: str = "mainnet"
    ) -> WalletBalance:
        """ERC-20 balance of an allowlisted token at the finalized block."""
        if not _ADDRESS.fullmatch(address):
            raise ValueError("Expected a lowercase 20-byte Ethereum hex address")
        asset = await self._ethereum_asset(network)
        chain_id, _ = ETHEREUM_NETWORKS[network]
        registered = allowlist_for_chain(chain_id).token(token)
        if registered is None:
            raise ValueError("Token is not on the audited allowlist for this chain")
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
        pinned: dict[str, object] = {"blockHash": block.hash, "requireCanonical": True}
        decimals = _call_word(
            await self._request(
                "eth_call",
                [
                    {"to": registered.address, "data": "0x" + SELECTOR_DECIMALS.hex()},
                    pinned,
                ],
            )
        )
        if decimals != registered.decimals:
            raise WalletUnavailable("Token decimals do not match the allowlist entry")
        calldata = "0x" + SELECTOR_BALANCE_OF.hex() + address[2:].rjust(64, "0")
        units = _call_word(
            await self._request(
                "eth_call", [{"to": registered.address, "data": calldata}, pinned]
            )
        )
        token_asset = f"eip155:{chain_id}/erc20:{registered.address}"
        del asset
        return WalletBalance(
            address,
            token_asset,
            str(units),
            registered.decimals,
            height,
            block.hash,
            datetime.now(UTC),
        )

    async def solana_token_balance(
        self, address: str, mint: str, *, network: str = "mainnet-beta"
    ) -> SolanaBalance:
        """Sum of the owner's SPL token accounts for one allowlisted mint."""
        if not _is_solana_address(address):
            raise ValueError("Expected a 32-byte Solana base58 address")
        if mint not in ALLOWED_SOLANA_MINTS or mint not in SOLANA_TOKEN_DECIMALS:
            raise ValueError("Mint is not on the audited allowlist")
        genesis, asset = await self._solana_identity(network)
        if network != "mainnet-beta":
            raise ValueError("SPL token reads are limited to mainnet-beta mints")
        try:
            response = await self._request(
                "getTokenAccountsByOwner",
                [
                    address,
                    {"mint": mint},
                    {"encoding": "jsonParsed", "commitment": "finalized"},
                ],
            )
            if not isinstance(response, dict):
                raise ValueError("unexpected token account response")
            context = _SlotContext.model_validate(response.get("context"), strict=True)
            values = response.get("value")
            if not isinstance(values, list) or len(values) > 64:
                raise ValueError("unexpected token account list")
            total = 0
            for value in values:
                entry = _TokenAccountValue.model_validate(value, strict=True)
                data = _ParsedTokenAccountData.model_validate(
                    entry.account.get("data"), strict=True
                )
                info = data.parsed.info
                if (
                    info.mint != mint
                    or info.tokenAmount.decimals != SOLANA_TOKEN_DECIMALS[mint]
                    or not info.tokenAmount.amount.isdecimal()
                ):
                    raise ValueError("token account does not match the request")
                total += int(info.tokenAmount.amount)
            if total >= 2**64:
                raise ValueError("token balance overflow")
        except ValueError:
            raise WalletUnavailable("Invalid Solana token account response") from None
        return SolanaBalance(
            address,
            f"{asset.network.value}/spl:{mint}",
            str(total),
            SOLANA_TOKEN_DECIMALS[mint],
            context.slot,
            genesis,
            datetime.now(UTC),
        )

    async def utxo_transaction(
        self, txid: str, network: UtxoNetwork
    ) -> TransactionObservation:
        """Mempool/confirmation/reorg state of a BTC-family transaction.

        Requires a node with ``-txindex`` (or the transaction in its mempool);
        otherwise the transaction is reported as ``unknown``.
        """
        if not _UTXO_HASH.fullmatch(txid):
            raise ValueError("Expected a 32-byte transaction id")
        txid = txid.lower()
        version = network.rpc_version
        identity = network.asset.network.value
        try:
            await self._verify_utxo_node(network)
            payload = await self._request(
                "getrawtransaction", [txid, True], version=version, not_found_ok=True
            )
            if payload is _NOT_FOUND or payload is None:
                return TransactionObservation(
                    identity, txid, datetime.now(UTC), "unknown"
                )
            raw = _UtxoRawTransaction.model_validate(payload, strict=True)
            if raw.txid.lower() != txid:
                raise WalletUnavailable(
                    f"{network.name} node returned a different transaction"
                )
            if raw.blockhash is None:
                if raw.confirmations not in (None, 0):
                    raise WalletUnavailable("Mempool transaction reports confirmations")
                return TransactionObservation(
                    identity, txid, datetime.now(UTC), "processed", confirmations=0
                )
            if not _UTXO_HASH.fullmatch(raw.blockhash):
                raise WalletUnavailable(f"Invalid {network.name} block hash")
            header = _UtxoBlockHeader.model_validate(
                await self._request(
                    "getblockheader", [raw.blockhash, True], version=version
                ),
                strict=True,
            )
        except ValueError:
            raise WalletUnavailable(
                f"Invalid {network.name} node transaction response"
            ) from None
        if header.hash.lower() != raw.blockhash.lower():
            raise WalletUnavailable(f"{network.name} block header does not match")
        if header.confirmations < 1:
            # The containing block is no longer on the best chain (reorg).
            return TransactionObservation(
                identity,
                txid,
                datetime.now(UTC),
                "unknown",
                block_number=header.height,
                block_hash=header.hash,
                confirmations=0,
            )
        confirmations = header.confirmations
        return TransactionObservation(
            identity,
            txid,
            datetime.now(UTC),
            "finalized" if confirmations >= UTXO_FINAL_CONFIRMATIONS else "confirmed",
            True,
            block_number=header.height,
            block_hash=header.hash,
            confirmations=confirmations,
        )

    async def ethereum_finalized_height(self, network: str = "mainnet") -> int:
        """Finalized block height after verifying the node's chain ID."""
        await self._ethereum_asset(network)
        try:
            block = _FinalizedBlock.model_validate(
                await self._request("eth_getBlockByNumber", ["finalized", False]),
                strict=True,
            )
        except ValueError:
            raise WalletUnavailable(
                "Local node did not return a finalized block"
            ) from None
        return _quantity(block.number)

    async def solana_finalized_slot(self, network: str = "mainnet-beta") -> int:
        """Finalized slot after verifying the cluster genesis hash."""
        await self._solana_identity(network)
        slot = await self._request("getSlot", [{"commitment": "finalized"}])
        if not isinstance(slot, int) or isinstance(slot, bool) or slot < 0:
            raise WalletUnavailable("Invalid finalized Solana slot")
        return slot

    async def utxo_height(self, network: UtxoNetwork) -> int:
        """Best block height after verifying chain identity and sync state."""
        try:
            await self._verify_utxo_node(network)
            count = await self._request(
                "getblockcount", [], version=network.rpc_version
            )
        except ValueError:
            raise WalletUnavailable(
                f"Invalid {network.name} node health response"
            ) from None
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise WalletUnavailable(f"Invalid {network.name} block count")
        return count

    async def _verify_utxo_node(self, network: UtxoNetwork) -> str:
        """Check chain identity, sync state and genesis; return the network label."""
        chain = _UtxoChain.model_validate(
            await self._request("getblockchaininfo", [], version=network.rpc_version),
            strict=True,
        )
        label = "mainnet" if network.chain == "main" else network.chain
        if chain.chain != network.chain or chain.initialblockdownload:
            raise WalletUnavailable(f"{network.name} node must report synced {label}")
        if network.signet_challenge is not None and (
            chain.signet_challenge != network.signet_challenge
        ):
            raise WalletUnavailable(
                "Local node does not match the default Signet challenge"
            )
        genesis = await self._request("getblockhash", [0], version=network.rpc_version)
        if genesis != network.genesis:
            raise WalletUnavailable(
                f"Local node does not match the {network.name} genesis"
            )
        return label

    async def utxo_balance(self, address: str, network: UtxoNetwork) -> WalletBalance:
        if not _UTXO_ADDRESS.fullmatch(address):
            raise ValueError(f"Expected a {network.name} address")
        try:
            label = await self._verify_utxo_node(network)
            validated = _ValidatedAddress.model_validate(
                await self._request(
                    "validateaddress", [address], version=network.rpc_version
                ),
                strict=True,
            )
            if not validated.isvalid:
                raise WalletUnavailable(
                    f"Address is invalid for {network.name} {label}"
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


def _call_word(value: object) -> int:
    """Parse a 32-byte eth_call return word."""
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
        raise WalletUnavailable("Invalid eth_call return word")
    return int(value, 16)


def _is_solana_address(address: str) -> bool:
    return _is_base58_size(address, 32)


def _is_base58_size(address: str, size: int) -> bool:
    if not size <= len(address) <= (size * 138 + 99) // 100 or any(
        char not in _BASE58 for char in address
    ):
        return False
    decoded = 0
    for char in address:
        decoded = decoded * 58 + _BASE58.index(char)
    leading_zero_bytes = len(address) - len(address.lstrip("1"))
    return leading_zero_bytes + (decoded.bit_length() + 7) // 8 == size


# --- ChainWallet adapters (AGENTS.md 6.1) -----------------------------------------

_SPEND_DISABLED = (
    "Direct on-chain spending is disabled in V1; this adapter is read-only"
)
_UTXO_CHAIN_NAMES = {
    "btc-balance": "bitcoin",
    "ltc-balance": "litecoin",
    "dgb-balance": "digibyte",
}


def _chain_balance(snapshot: WalletBalance | SolanaBalance) -> ChainBalance:
    if isinstance(snapshot, WalletBalance):
        height, reference = snapshot.block_number, snapshot.block_hash
    else:
        height, reference = snapshot.slot, snapshot.genesis_hash
    return ChainBalance(
        snapshot.address,
        snapshot.asset,
        snapshot.base_units,
        snapshot.decimals,
        height,
        reference,
        snapshot.observed_at,
    )


class _ReadOnlyChainWallet:
    """Shared behaviour: configured addresses, and spend operations unsupported."""

    network: str

    def __init__(self, rpc: LocalWalletRpc, addresses: Sequence[WalletAddress]) -> None:
        if not addresses:
            raise ValueError("At least one address is required")
        self._rpc = rpc
        self._addresses = tuple(addresses)

    async def get_addresses(self) -> Sequence[WalletAddress]:
        return self._addresses

    async def build_preview(self, intent: TransferOrSwapIntent) -> UnsignedTransaction:
        raise WalletOperationUnsupported(_SPEND_DISABLED)

    async def simulate(self, tx: UnsignedTransaction) -> SimulationResult:
        raise WalletOperationUnsupported(_SPEND_DISABLED)

    async def broadcast(self, signed_tx: SignedTransaction) -> BroadcastResult:
        raise WalletOperationUnsupported(_SPEND_DISABLED)

    def _health(self, height: int | None, detail: str) -> ChainHealth:
        return ChainHealth(
            self.network, height is not None, detail, height, datetime.now(UTC)
        )


def _known(observation: TransactionObservation) -> TransactionObservation | None:
    return None if observation.finality == "unknown" else observation


class EthereumChainWallet(_ReadOnlyChainWallet):
    """ETH and allowlisted ERC-20 balances plus receipt status on one EVM chain."""

    def __init__(
        self,
        rpc: LocalWalletRpc,
        addresses: Sequence[str],
        *,
        network: str = "mainnet",
        role: WalletRole = "watch_only",
    ) -> None:
        if network not in ETHEREUM_NETWORKS:
            raise ValueError("Unsupported Ethereum network")
        self._network_name = network
        self._chain_id = ETHEREUM_NETWORKS[network][0]
        self.network = f"eip155:{self._chain_id}"
        super().__init__(
            rpc,
            [
                WalletAddress(validate_evm_address(a).normalized, self.network, role)
                for a in addresses
            ],
        )

    async def get_balances(self) -> Sequence[ChainBalance]:
        balances: list[ChainBalance] = []
        tokens = allowlist_for_chain(self._chain_id).tokens
        for wallet in self._addresses:
            balances.append(
                _chain_balance(
                    await self._rpc.ethereum_balance(
                        wallet.address, network=self._network_name
                    )
                )
            )
            for token in tokens:
                balances.append(
                    _chain_balance(
                        await self._rpc.ethereum_token_balance(
                            wallet.address, token.address, network=self._network_name
                        )
                    )
                )
        return balances

    async def get_transaction(self, tx_id: str) -> TransactionObservation | None:
        return _known(
            await self._rpc.ethereum_transaction(tx_id, network=self._network_name)
        )

    async def health(self) -> ChainHealth:
        try:
            height = await self._rpc.ethereum_finalized_height(self._network_name)
        except WalletUnavailable as error:
            return self._health(None, str(error))
        return self._health(height, f"finalized block {height}")


class SolanaChainWallet(_ReadOnlyChainWallet):
    """SOL and allowlisted SPL (USDC/USDT) balances plus signature status."""

    def __init__(
        self,
        rpc: LocalWalletRpc,
        addresses: Sequence[str],
        *,
        network: str = "mainnet-beta",
        role: WalletRole = "watch_only",
    ) -> None:
        if network not in SOLANA_NETWORKS:
            raise ValueError("Unsupported Solana network")
        for address in addresses:
            if not _is_solana_address(address):
                raise ValueError("Expected a 32-byte Solana base58 address")
        self._network_name = network
        self.network = SOLANA_NETWORKS[network][1].network.value
        super().__init__(rpc, [WalletAddress(a, self.network, role) for a in addresses])

    async def get_balances(self) -> Sequence[ChainBalance]:
        balances: list[ChainBalance] = []
        spl_mints = sorted(
            m for m in ALLOWED_SOLANA_MINTS if not m.startswith("So1111")
        )
        for wallet in self._addresses:
            balances.append(
                _chain_balance(
                    await self._rpc.solana_balance(
                        wallet.address, network=self._network_name
                    )
                )
            )
            if self._network_name != "mainnet-beta":
                continue
            for mint in spl_mints:
                balances.append(
                    _chain_balance(
                        await self._rpc.solana_token_balance(
                            wallet.address, mint, network=self._network_name
                        )
                    )
                )
        return balances

    async def get_transaction(self, tx_id: str) -> TransactionObservation | None:
        return _known(
            await self._rpc.solana_transaction(tx_id, network=self._network_name)
        )

    async def health(self) -> ChainHealth:
        try:
            slot = await self._rpc.solana_finalized_slot(self._network_name)
        except WalletUnavailable as error:
            return self._health(None, str(error))
        return self._health(slot, f"finalized slot {slot}")


class UtxoChainWallet(_ReadOnlyChainWallet):
    """BTC, LTC or DGB address balances and transaction status via a local node."""

    def __init__(
        self,
        rpc: LocalWalletRpc,
        command: str,
        addresses: Sequence[str],
        *,
        profile: str = "mainnet",
        role: WalletRole = "watch_only",
    ) -> None:
        networks = UTXO_NETWORK_PROFILES.get(command)
        if networks is None or profile not in networks:
            raise ValueError("Unsupported UTXO network")
        self._utxo = networks[profile]
        chain = _UTXO_CHAIN_NAMES[command]
        self.network = self._utxo.asset.network.value
        for address in addresses:
            if has_offline_profile(chain, profile):
                validate_utxo_address(chain, address, profile)
            elif not _UTXO_ADDRESS.fullmatch(address):
                raise ValueError(f"Expected a {self._utxo.name} address")
        super().__init__(rpc, [WalletAddress(a, self.network, role) for a in addresses])

    async def get_balances(self) -> Sequence[ChainBalance]:
        return [
            _chain_balance(await self._rpc.utxo_balance(a.address, self._utxo))
            for a in self._addresses
        ]

    async def get_transaction(self, tx_id: str) -> TransactionObservation | None:
        return _known(await self._rpc.utxo_transaction(tx_id, self._utxo))

    async def health(self) -> ChainHealth:
        try:
            height = await self._rpc.utxo_height(self._utxo)
        except WalletUnavailable as error:
            return self._health(None, str(error))
        return self._health(height, f"best block {height}")
