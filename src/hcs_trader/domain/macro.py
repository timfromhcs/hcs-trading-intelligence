"""Macroeconomic values with separate observation and availability dates."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class MacroObservation:
    source: str
    series: str
    observation_date: date
    received_at: datetime
    value: Decimal
