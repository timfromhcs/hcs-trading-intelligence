"""Public-address balance snapshots from a specific chain state."""

from dataclasses import dataclass
from datetime import datetime


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
