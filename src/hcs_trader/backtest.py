"""Deterministic research backtest for a long-only SMA trend strategy."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Context, Decimal, localcontext
from hashlib import sha256
from itertools import pairwise
from random import Random
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
    dataset_opened_at: datetime
    dataset_closed_at: datetime
    config: BacktestConfig
    ending_cash: Decimal
    ending_quantity: Decimal
    ending_equity: Decimal
    net_return: Decimal
    max_drawdown: Decimal
    total_fees: Decimal
    fills: tuple[SimulatedFill, ...]


def run_backtest(
    candles: Sequence[Candle],
    config: BacktestConfig,
    *,
    trade_start: int | None = None,
    buy_and_hold: bool = False,
) -> BacktestResult:
    return _run(candles, config, trade_start, buy_and_hold, None).result


@dataclass(frozen=True, slots=True)
class _Replay:
    result: BacktestResult
    start: int
    equity: tuple[Decimal, ...]
    exposed: tuple[bool, ...]
    slippage_cost: Decimal


def _run(
    candles: Sequence[Candle],
    config: BacktestConfig,
    trade_start: int | None,
    buy_and_hold: bool,
    stop_loss_pct: Decimal | None,
) -> _Replay:
    start = config.slow_period if trade_start is None else trade_start
    if not config.slow_period <= start < len(candles):
        raise ValueError(
            "Need a full strategy warm-up and at least one execution candle"
        )
    if stop_loss_pct is not None and not 0 < stop_loss_pct < 1:
        raise ValueError("stop_loss_pct must be between zero and one")
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
        return _replay(
            candles, config, dataset.hexdigest(), start, buy_and_hold, stop_loss_pct
        )


def _replay(
    candles: Sequence[Candle],
    config: BacktestConfig,
    dataset_hash: str,
    start: int,
    buy_and_hold: bool,
    stop_loss_pct: Decimal | None = None,
) -> _Replay:
    cash = peak = equity = config.starting_cash
    quantity = drawdown = fees = slippage_cost = entry_price = _ZERO
    fills = []
    equity_curve: list[Decimal] = []
    exposed: list[bool] = []
    closes: list[Decimal] = []
    fee_rate = config.fee_bps / 10_000
    impact = (config.spread_bps / 2 + config.slippage_bps) / 10_000

    for index, candle in enumerate(candles):
        if index >= start:
            fast = sum(closes[-config.fast_period :], _ZERO) / config.fast_period
            slow = sum(closes[-config.slow_period :], _ZERO) / config.slow_period
            previous = candles[index - 1]
            side: Literal["buy", "sell"] = (
                "buy" if buy_and_hold or fast > slow else "sell"
            )
            reference = candle.open
            if stop_loss_pct is not None and quantity:
                stop_price = entry_price * (1 - stop_loss_pct)
                if candle.open <= stop_price:
                    # Gap through the stop: it fills at the open, not the stop.
                    side = "sell"
                elif candle.low <= stop_price:
                    side, reference = "sell", stop_price
            price = reference * (1 + impact if side == "buy" else 1 - impact)
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
                slippage_cost += filled * reference * impact
                if side == "buy":
                    entry_price = (entry_price * quantity + price * filled) / (
                        quantity + filled
                    )
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
        equity_curve.append(equity)
        exposed.append(quantity > 0)
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
        closes.append(candle.close)

    result = BacktestResult(
        "buy-and-hold" if buy_and_hold else STRATEGY,
        dataset_hash,
        len(candles),
        candles[0].opened_at,
        candles[-1].closed_at,
        config,
        cash,
        quantity,
        equity,
        equity / config.starting_cash - 1,
        drawdown,
        fees,
        tuple(fills),
    )
    return _Replay(result, start, tuple(equity_curve), tuple(exposed), slippage_cost)


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    """Reporting metrics derived from the equity curve and fills (AGENTS 5.5)."""

    periods: int
    sharpe: Decimal | None
    sortino: Decimal | None
    calmar: Decimal | None
    annualized_return: Decimal
    trade_count: int
    win_rate: Decimal | None
    average_win: Decimal | None
    average_loss: Decimal | None
    expectancy: Decimal | None
    exposure: Decimal
    turnover: Decimal
    total_fees: Decimal
    total_slippage: Decimal
    worst_period_return: Decimal
    benchmark_return: Decimal
    excess_return: Decimal


@dataclass(frozen=True, slots=True)
class BootstrapSummary:
    seed: int
    iterations: int
    p05: Decimal
    median: Decimal
    p95: Decimal
    loss_probability: Decimal


@dataclass(frozen=True, slots=True)
class BacktestAnalysis:
    result: BacktestResult
    benchmark: BacktestResult
    metrics: BacktestMetrics
    bootstrap: BootstrapSummary | None


def analyze_backtest(
    candles: Sequence[Candle],
    config: BacktestConfig,
    *,
    trade_start: int | None = None,
    stop_loss_pct: Decimal | None = None,
    bootstrap_iterations: int = 0,
    bootstrap_seed: int = 0,
) -> BacktestAnalysis:
    """Run the strategy plus buy-and-hold and derive deterministic metrics.

    ``stop_loss_pct`` adds a stop that fills at the open when a candle gaps
    through it. All orders are modelled as taker market orders at ``fee_bps``.
    """
    if not 0 <= bootstrap_iterations <= 10_000:
        raise ValueError("bootstrap_iterations must be between 0 and 10000")
    replay = _run(candles, config, trade_start, False, stop_loss_pct)
    benchmark = _run(candles, config, trade_start, True, None).result
    with localcontext(Context(prec=34)):
        returns = _period_returns(config.starting_cash, replay)
        metrics = _metrics(config, replay, returns, benchmark, candles[0])
        summary = (
            _bootstrap(returns, bootstrap_iterations, bootstrap_seed)
            if bootstrap_iterations
            else None
        )
    return BacktestAnalysis(replay.result, benchmark, metrics, summary)


def _base_equity(starting_cash: Decimal, replay: _Replay) -> Decimal:
    return replay.equity[replay.start - 1] if replay.start else starting_cash


def _period_returns(starting_cash: Decimal, replay: _Replay) -> list[Decimal]:
    curve = (_base_equity(starting_cash, replay), *replay.equity[replay.start :])
    return [value / previous - 1 for previous, value in pairwise(curve)]


def _metrics(
    config: BacktestConfig,
    replay: _Replay,
    returns: list[Decimal],
    benchmark: BacktestResult,
    first: Candle,
) -> BacktestMetrics:
    result = replay.result
    per_year = 525_600 // first.interval_minutes
    count = len(returns)
    mean = sum(returns, _ZERO) / count
    variance = sum(((r - mean) ** 2 for r in returns), _ZERO) / count
    downside = sum((min(r, _ZERO) ** 2 for r in returns), _ZERO) / count
    root = Decimal(per_year).sqrt()
    window_return = replay.equity[-1] / _base_equity(config.starting_cash, replay) - 1
    annualized = (
        ((1 + window_return).ln() * per_year / count).exp() - 1
        if window_return > -1
        else Decimal(-1)
    )

    # Round trips by average cost, net of both legs' fees.
    pnls: list[Decimal] = []
    quantity = cost = traded = _ZERO
    for fill in result.fills:
        traded += fill.quantity * fill.price
        if fill.side == "buy":
            quantity += fill.quantity
            cost += fill.quantity * fill.price + fill.fee
        else:
            portion = cost * fill.quantity / quantity
            pnls.append(fill.quantity * fill.price - fill.fee - portion)
            cost -= portion
            quantity -= fill.quantity
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    average_equity = sum(replay.equity[replay.start :], _ZERO) / count
    return BacktestMetrics(
        periods=count,
        sharpe=mean / variance.sqrt() * root if variance > 0 else None,
        sortino=mean / downside.sqrt() * root if downside > 0 else None,
        calmar=annualized / result.max_drawdown if result.max_drawdown > 0 else None,
        annualized_return=annualized,
        trade_count=len(pnls),
        win_rate=Decimal(len(wins)) / len(pnls) if pnls else None,
        average_win=sum(wins, _ZERO) / len(wins) if wins else None,
        average_loss=sum(losses, _ZERO) / len(losses) if losses else None,
        expectancy=sum(pnls, _ZERO) / len(pnls) if pnls else None,
        exposure=Decimal(sum(replay.exposed[replay.start :])) / count,
        turnover=traded / average_equity,
        total_fees=result.total_fees,
        total_slippage=replay.slippage_cost,
        worst_period_return=min(returns),
        benchmark_return=benchmark.net_return,
        excess_return=result.net_return - benchmark.net_return,
    )


def _bootstrap(returns: list[Decimal], iterations: int, seed: int) -> BootstrapSummary:
    rng = Random(seed)  # noqa: S311 - seeded, reproducible research statistics
    outcomes = []
    for _ in range(iterations):
        growth = Decimal(1)
        for value in rng.choices(returns, k=len(returns)):
            growth *= 1 + value
        outcomes.append(growth - 1)
    outcomes.sort()

    def pick(fraction: Decimal) -> Decimal:
        return outcomes[min(iterations - 1, int(fraction * iterations))]

    return BootstrapSummary(
        seed,
        iterations,
        pick(Decimal("0.05")),
        pick(Decimal("0.5")),
        pick(Decimal("0.95")),
        Decimal(sum(1 for value in outcomes if value < 0)) / iterations,
    )
