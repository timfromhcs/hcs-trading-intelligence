"""Deterministic Decimal indicators over contiguous, closed candles."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Context, Decimal, localcontext
from itertools import pairwise
from typing import Literal

from hcs_trader.domain.market_data import Candle

_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class Indicators:
    contiguous_candles: int
    closed_at: datetime | None = None
    rsi14: Decimal | None = None
    sma20: Decimal | None = None
    ema20: Decimal | None = None
    macd: Decimal | None = None
    macd_signal: Decimal | None = None
    macd_histogram: Decimal | None = None
    atr14: Decimal | None = None
    bollinger_upper: Decimal | None = None
    bollinger_lower: Decimal | None = None
    volatility20: Decimal | None = None
    volume_sma20: Decimal | None = None
    source: str | None = None
    interval_minutes: int | None = None
    opened_at: datetime | None = None


def calculate_indicators(candles: Sequence[Candle]) -> Indicators:
    if not candles:
        return Indicators(contiguous_candles=0)

    segment_start = 0
    for index, (previous, current) in enumerate(pairwise(candles), start=1):
        if (
            previous.market != current.market
            or previous.source != current.source
            or previous.interval_minutes != current.interval_minutes
        ):
            raise ValueError("Indicators require one source, market and interval")
        if previous.closed_at > current.opened_at:
            raise ValueError("Indicators require ordered, non-overlapping candles")
        if previous.closed_at < current.opened_at:
            segment_start = index

    segment = candles[segment_start:]
    closes = [candle.close for candle in segment]
    with localcontext(Context(prec=34)):
        returns = [close / previous - 1 for previous, close in pairwise(closes)]
        sma = _mean(closes[-20:]) if len(closes) >= 20 else None
        spread = _stddev(closes[-20:]) * 2 if sma is not None else None
        macd, signal, histogram = _macd(closes)
        return Indicators(
            contiguous_candles=len(segment),
            closed_at=segment[-1].closed_at,
            source=segment[-1].source,
            interval_minutes=segment[-1].interval_minutes,
            opened_at=segment[0].opened_at,
            rsi14=_rsi(closes, 14),
            sma20=sma,
            ema20=_ema(closes, 20)[-1] if sma is not None else None,
            macd=macd,
            macd_signal=signal,
            macd_histogram=histogram,
            atr14=_atr(segment, 14),
            bollinger_upper=sma + spread
            if sma is not None and spread is not None
            else None,
            bollinger_lower=sma - spread
            if sma is not None and spread is not None
            else None,
            volatility20=_stddev(returns[-20:]) if len(returns) >= 20 else None,
            volume_sma20=_mean([candle.volume for candle in segment[-20:]])
            if sma is not None
            else None,
        )


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, _ZERO) / len(values)


def _stddev(values: Sequence[Decimal]) -> Decimal:
    average = _mean(values)
    return _mean([(value - average) ** 2 for value in values]).sqrt()


def _rsi(closes: Sequence[Decimal], period: int) -> Decimal | None:
    if len(closes) <= period:
        return None
    changes = [close - previous for previous, close in pairwise(closes)]
    gain = _mean([max(change, _ZERO) for change in changes[:period]])
    loss = _mean([max(-change, _ZERO) for change in changes[:period]])
    for change in changes[period:]:
        gain = (gain * (period - 1) + max(change, _ZERO)) / period
        loss = (loss * (period - 1) + max(-change, _ZERO)) / period
    return gain * 100 / (gain + loss) if gain + loss else Decimal(50)


def _atr(candles: Sequence[Candle], period: int) -> Decimal | None:
    if len(candles) <= period:
        return None
    ranges = [
        max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        for previous, current in pairwise(candles)
    ]
    result = _mean(ranges[:period])
    for value in ranges[period:]:
        result = (result * (period - 1) + value) / period
    return result


def _ema(values: Sequence[Decimal], period: int) -> list[Decimal]:
    alpha = Decimal(2) / (period + 1)
    result = [_mean(values[:period])]
    for value in values[period:]:
        result.append(result[-1] + alpha * (value - result[-1]))
    return result


def _macd(
    closes: Sequence[Decimal],
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    if len(closes) < 34:
        return None, None, None
    # Seed the fast and slow averages on the same candle.
    fast = _ema(closes[26 - 12 :], 12)
    slow = _ema(closes, 26)
    values = [quick - long for quick, long in zip(fast, slow, strict=True)]
    signal = _ema(values, 9)[-1]
    return values[-1], signal, values[-1] - signal


# --- Configurable public indicator API (AGENTS 5.1) -------------------------
# Edge-case contract, tested explicitly:
#   * insufficient history -> None (never a made-up value)
#   * RSI: all gains -> 100, all losses -> 0, flat series -> 50 (neutral by
#     convention; Wilder's ratio is 0/0)
#   * every function requires finite Decimals and a period >= 1


def _check(values: Sequence[Decimal], period: int) -> None:
    if period < 1:
        raise ValueError("period must be at least 1")
    if any(not isinstance(value, Decimal) or not value.is_finite() for value in values):
        raise ValueError("Indicators require finite Decimal inputs")


def sma(values: Sequence[Decimal], period: int) -> Decimal | None:
    _check(values, period)
    if len(values) < period:
        return None
    with localcontext(Context(prec=34)):
        return _mean(values[-period:])


def ema(values: Sequence[Decimal], period: int) -> Decimal | None:
    """EMA seeded with the SMA of the first ``period`` values."""
    _check(values, period)
    if len(values) < period:
        return None
    with localcontext(Context(prec=34)):
        return _ema(values, period)[-1]


def rsi(values: Sequence[Decimal], period: int = 14) -> Decimal | None:
    _check(values, period)
    with localcontext(Context(prec=34)):
        return _rsi(values, period)


def atr(candles: Sequence[Candle], period: int = 14) -> Decimal | None:
    if period < 1:
        raise ValueError("period must be at least 1")
    with localcontext(Context(prec=34)):
        return _atr(candles, period)


def normalized_atr(candles: Sequence[Candle], period: int = 14) -> Decimal | None:
    """ATR as a fraction of the latest close."""
    value = atr(candles, period)
    if value is None or candles[-1].close <= 0:
        return None
    with localcontext(Context(prec=34)):
        return value / candles[-1].close


def macd(
    values: Sequence[Decimal], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[Decimal, Decimal, Decimal] | None:
    _check(values, fast)
    if not 1 <= fast < slow or signal < 1:
        raise ValueError("Require 1 <= fast < slow and signal >= 1")
    if len(values) < slow + signal - 1:
        return None
    with localcontext(Context(prec=34)):
        quick = _ema(values[slow - fast :], fast)
        long = _ema(values, slow)
        line = [a - b for a, b in zip(quick, long, strict=True)]
        trigger = _ema(line, signal)[-1]
        return line[-1], trigger, line[-1] - trigger


def bollinger(
    values: Sequence[Decimal], period: int = 20, width: Decimal = Decimal(2)
) -> tuple[Decimal, Decimal, Decimal] | None:
    """Return (lower, middle, upper) using population standard deviation."""
    _check(values, period)
    if len(values) < period:
        return None
    with localcontext(Context(prec=34)):
        middle = _mean(values[-period:])
        spread = _stddev(values[-period:]) * width
        return middle - spread, middle, middle + spread


def volume_change(candles: Sequence[Candle], period: int = 20) -> Decimal | None:
    """Latest volume relative to its trailing mean (1 = unchanged)."""
    if period < 1:
        raise ValueError("period must be at least 1")
    if len(candles) < period:
        return None
    with localcontext(Context(prec=34)):
        average = _mean([candle.volume for candle in candles[-period:]])
        return candles[-1].volume / average if average > 0 else None


def trend_state(
    values: Sequence[Decimal], fast: int = 10, slow: int = 20
) -> Literal["up", "down", "flat"] | None:
    """Coarse market-structure feature: relation of fast SMA to slow SMA."""
    quick, long = sma(values, fast), sma(values, slow)
    if quick is None or long is None:
        return None
    return "up" if quick > long else "down" if quick < long else "flat"


@dataclass(frozen=True, slots=True)
class WarmupStatus:
    """What the model and strategies may rely on (AGENTS 2.5, 5.1)."""

    contiguous_candles: int
    rsi14: bool
    sma20: bool
    macd: bool
    atr14: bool
    bollinger20: bool
    complete: bool


def warmup_status(contiguous_candles: int) -> WarmupStatus:
    count = contiguous_candles
    flags = (count > 14, count >= 20, count >= 34, count > 14, count >= 20)
    return WarmupStatus(count, *flags, complete=all(flags))
