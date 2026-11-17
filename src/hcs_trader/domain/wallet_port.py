"""Chain wallet port (AGENTS.md 6.1): typed, chain-agnostic, spend-disabled in V1."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from hcs_trader.domain.wallet import TransactionObservation

WalletRole = Literal["hot", "cold", "watch_only"]
# Only a deliberately funded hot wallet may ever hold signing material.
SIGNABLE_WALLET_ROLES: frozenset[str] = frozenset({"hot"})


class WalletOperationUnsupported(Exception):
    """The adapter does not implement this operation in the current release."""


@dataclass(frozen=True, slots=True)
class WalletAddress:
    address: str
    network: str
    role: WalletRole = "watch_only"


@dataclass(frozen=True, slots=True)
class ChainBalance:
    """One asset balance pinned to the chain state it was read from."""

    address: str
    asset: str
    base_units: str
    decimals: int
    height: int
    reference: str  # block hash (EVM/UTXO) or genesis hash (Solana)
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class ChainHealth:
    network: str
    healthy: bool
    detail: str
    height: int | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class TransferOrSwapIntent:
    network: str
    asset: str
    amount_base_units: int
    destination: str


@dataclass(frozen=True, slots=True)
class UnsignedTransaction:
    network: str
    payload: bytes
    plan_hash: str


@dataclass(frozen=True, slots=True)
class SimulationResult:
    success: bool
    detail: str


@dataclass(frozen=True, slots=True)
class SignedTransaction:
    network: str
    raw: bytes


@dataclass(frozen=True, slots=True)
class BroadcastResult:
    network: str
    identifier: str


class ChainWallet(Protocol):
    """Common port. Chain-specific semantics stay inside each adapter."""

    network: str

    async def get_addresses(self) -> Sequence[WalletAddress]: ...

    async def get_balances(self) -> Sequence[ChainBalance]: ...

    async def get_transaction(self, tx_id: str) -> TransactionObservation | None: ...

    async def build_preview(
        self, intent: TransferOrSwapIntent
    ) -> UnsignedTransaction: ...

    async def simulate(self, tx: UnsignedTransaction) -> SimulationResult: ...

    async def broadcast(self, signed_tx: SignedTransaction) -> BroadcastResult: ...

    async def health(self) -> ChainHealth: ...
