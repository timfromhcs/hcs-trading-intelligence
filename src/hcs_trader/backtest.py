"""Deterministic research backtest for a long-only SMA trend strategy."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Context, Decimal, localcontext
from hashlib import sha256
from itertools import pairwise
from typing import Literal

from hcs_trader.domain.market_data import Candle

STRATEGY = "sma-trend-1"
_ZERO = Decimal(0)
_QUANTITY_STEP = Decimal("0.00000001")
_MAX_NOTIONAL = Decimal("1e12")


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    starting_cash: Decimal = Decimal("10000")
    fast_period: int = 10
    slow_period: int = 20
    fee_bps: Decimal = Decimal("25")
    spread_bps: Decimal = Decimal("10")
    slippage_bps: Decimal = Decimal("5")
    allocation: Decimal = Decimal("0.10")
    volume_fraction: Decimal = Decimal("0.01")
    min_notional: Decimal = Decimal("10")

    def __post_init__(self) -> None:
        if not 2 <= self.fast_period < self.slow_period <= 200:
            raise ValueError("Require 2 <= fast_period < slow_period <= 200")
        for name in ("starting_cash", "min_notional"):
            value = getattr(self, name)
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or not 0 < value <= _MAX_NOTIONAL
            ):
                raise ValueError(f"{name} must be a positive Decimal at most 10^12")
        for name in ("fee_bps", "spread_bps", "slippage_bps"):
            value = getattr(self, name)
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or not 0 <= value <= 500
            ):
                raise ValueError(f"{name} must be between 0 and 500 basis points")
        for name in ("allocation", "volume_fraction"):
            value = getattr(self, name)
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or not 0 < value <= 1
            ):
                raise ValueError(f"{name} must be greater than zero and at most one")


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    side: Literal["buy", "sell"]
    signal_at: datetime
    filled_at: datetime
    requested_quantity: Decimal
    quantity: Decimal
    price: Decimal
    fee: Decimal
    partial: bool


@dataclass(frozen=True, slots=True)
class BacktestResult:
    strategy: str
    dataset_sha256: str
    candle_count: int
    config: BacktestConfig
    ending_cash: Decimal
    ending_quantity: Decimal
    ending_equity: Decimal
    net_return: Decimal
    max_drawdown: Decimal
    total_fees: Decimal
    fills: tuple[SimulatedFill, ...]


def run_backtest(candles: Sequence[Candle], config: BacktestConfig) -> BacktestResult:
    if len(candles) < config.slow_period + 1:
        raise ValueError(
            "Need a full strategy warm-up and at least one execution candle"
        )
    for previous, current in pairwise(candles):
        if (
            previous.market != current.market
            or previous.source != current.source
            or previous.interval_minutes != current.interval_minutes
            or previous.closed_at != current.opened_at
        ):
            raise ValueError("Backtests require a single contiguous candle series")

    dataset = sha256()
    for candle in candles:
        record = (
            candle.market.venue,
            str(candle.market.base),
            str(candle.market.quote),
            candle.source,
            str(candle.interval_minutes),
            candle.opened_at.isoformat(),
            str(candle.open),
            str(candle.high),
            str(candle.low),
            str(candle.close),
            str(candle.volume),
            str(candle.trade_count),
        )
        dataset.update(repr(record).encode("utf-8"))

    with localcontext(Context(prec=34)):
        return _replay(candles, config, dataset.hexdigest())


def _replay(
    candles: Sequence[Candle], config: BacktestConfig, dataset_hash: str
) -> BacktestResult:
    cash = peak = equity = config.starting_cash
    quantity = drawdown = fees = _ZERO
    fills = []
    closes: list[Decimal] = []
    fee_rate = config.fee_bps / 10_000
    impact = (config.spread_bps / 2 + config.slippage_bps) / 10_000

    for index, candle in enumerate(candles):
        if len(closes) >= config.slow_period:
            fast = sum(closes[-config.fast_period :], _ZERO) / config.fast_period
            slow = sum(closes[-config.slow_period :], _ZERO) / config.slow_period
            previous = candles[index - 1]
            side: Literal["buy", "sell"] = "buy" if fast > slow else "sell"
            price = candle.open * (1 + impact if side == "buy" else 1 - impact)
            if side == "buy":
                requested = (
                    cash * config.allocation / (price * (1 + fee_rate))
                    if not quantity
                    else _ZERO
                )
            else:
                requested = quantity
            requested = requested.quantize(_QUANTITY_STEP, rounding=ROUND_DOWN)
            # Only previous-candle volume was known when this order was formed.
            filled = min(requested, previous.volume * config.volume_fraction)
            filled = filled.quantize(_QUANTITY_STEP, rounding=ROUND_DOWN)
            notional = filled * price
            if notional >= config.min_notional:
                fee = notional * fee_rate
                if side == "buy":
                    cash -= notional + fee
                    quantity += filled
                else:
                    cash += notional - fee
                    quantity -= filled
                fees += fee
                fills.append(
                    SimulatedFill(
                        side,
                        previous.closed_at,
                        candle.opened_at,
                        requested,
                        filled,
                        price,
                        fee,
                        filled < requested,
                    )
                )

        equity = cash + quantity * candle.close
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
        closes.append(candle.close)

    return BacktestResult(
        STRATEGY,
        dataset_hash,
        len(candles),
        config,
        cash,
        quantity,
        equity,
        equity / config.starting_cash - 1,
        drawdown,
        fees,
        tuple(fills),
    )
