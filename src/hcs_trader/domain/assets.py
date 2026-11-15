"""Canonical identifiers and exact monetary types."""

from dataclasses import dataclass
from decimal import Decimal
from re import fullmatch


@dataclass(frozen=True, slots=True)
class NetworkId:
    value: str

    def __post_init__(self) -> None:
        if not fullmatch(r"[a-z0-9-]{1,32}:[A-Za-z0-9_-]{1,64}", self.value):
            raise ValueError("Expected a canonical namespace and network reference")


@dataclass(frozen=True, slots=True)
class AssetId:
    network: NetworkId
    namespace: str
    reference: str

    def __post_init__(self) -> None:
        if not fullmatch(r"[a-z0-9-]{1,32}", self.namespace):
            raise ValueError("Invalid asset namespace")
        if not fullmatch(r"[A-Za-z0-9_.%-]{1,128}", self.reference):
            raise ValueError("Invalid asset reference")

    def __str__(self) -> str:
        return f"{self.network.value}/{self.namespace}:{self.reference}"

    @classmethod
    def parse(cls, value: str) -> "AssetId":
        network, path = value.split("/", 1)
        namespace, reference = path.split(":", 1)
        return cls(NetworkId(network), namespace, reference)


@dataclass(frozen=True, slots=True)
class MarketId:
    venue: str
    base: AssetId
    quote: AssetId

    def __post_init__(self) -> None:
        if not fullmatch(r"[a-z0-9-]{1,32}", self.venue):
            raise ValueError("Invalid venue identifier")
        if self.base == self.quote:
            raise ValueError("Market base and quote assets must differ")


@dataclass(frozen=True, slots=True)
class Money:
    asset: AssetId
    amount: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal) or not self.amount.is_finite():
            raise ValueError("Money requires a finite Decimal")


@dataclass(frozen=True, slots=True)
class Quantity:
    asset: AssetId
    base_units: int

    def __post_init__(self) -> None:
        if type(self.base_units) is not int or self.base_units < 0:
            raise ValueError("Quantity requires non-negative integer base units")
