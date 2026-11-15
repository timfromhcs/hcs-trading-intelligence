"""Deterministic Decimal indicators over contiguous, closed candles."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Context, Decimal, localcontext
from itertools import pairwise

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
