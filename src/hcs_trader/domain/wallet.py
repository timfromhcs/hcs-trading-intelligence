"""Public-address balance snapshots from a specific chain state."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True, slots=True)
class WalletBalance:
    address: str
    asset: str
    base_units: str
    decimals: int
    block_number: int
    block_hash: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class SolanaBalance:
    address: str
    asset: str
    base_units: str
    decimals: int
    slot: int
    genesis_hash: str
    observed_at: datetime


type WalletSnapshot = WalletBalance | SolanaBalance


@dataclass(frozen=True, slots=True)
class TransactionObservation:
    network: str
    identifier: str
    observed_at: datetime
    finality: Literal["unknown", "processed", "confirmed", "finalized"]
    successful: bool | None = None
    block_number: int | None = None
    block_hash: str | None = None
    slot: int | None = None
    confirmations: int | None = None
