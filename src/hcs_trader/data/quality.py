"""Shared, deterministic data-quality checks for market data.

One implementation is used by ingestion, backtesting, paper execution and the
risk engine. All functions are pure: they never fetch, store or log anything.
A report never upgrades data; the worst finding decides the status.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from hcs_trader.domain.market_data import Candle, DataQuality, Quote


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    severity: Severity
    detail: str


@dataclass(frozen=True, slots=True)
class QualityReport:
    status: DataQuality
    findings: tuple[Finding, ...] = ()

    @property
    def tradable(self) -> bool:
        """Only unambiguously fresh and valid data may authorize an order."""
        return self.status is DataQuality.FRESH

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(finding.code for finding in self.findings)


def _report(findings: Sequence[Finding], *, missing: bool = False) -> QualityReport:
    if any(f.severity is Severity.ERROR for f in findings):
        status = DataQuality.INVALID
    elif missing:
        status = DataQuality.MISSING
    elif any(f.code == "stale" for f in findings):
        status = DataQuality.STALE
    elif any(f.severity is Severity.WARNING for f in findings):
        status = DataQuality.DEGRADED
    else:
        status = DataQuality.FRESH
    return QualityReport(status, tuple(findings))


def _error(code: str, detail: str) -> Finding:
    return Finding(code, Severity.ERROR, detail)


def _warning(code: str, detail: str) -> Finding:
    return Finding(code, Severity.WARNING, detail)


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("Timestamps must be timezone-aware UTC")


def check_freshness(
    observed_at: datetime,
    received_at: datetime,
    now: datetime,
    *,
    max_age: timedelta,
    max_clock_skew: timedelta = timedelta(0),
) -> QualityReport:
    """Check age and clock drift of one observation.

    A future receipt or a provider time ahead of receipt beyond the configured
    skew is invalid, not stale: it indicates a broken or hostile clock.
    """
    for value in (observed_at, received_at, now):
        _require_utc(value)
    if max_age <= timedelta(0) or max_clock_skew < timedelta(0):
        raise ValueError("Freshness requires positive age and non-negative skew")
    findings: list[Finding] = []
    if observed_at - received_at > max_clock_skew or received_at > now:
        findings.append(
            _error("clock_drift", "Observation is in the future relative to receipt")
        )
    age = now - min(observed_at, received_at)
    if age > max_age:
        findings.append(_warning("stale", f"Observation is {age} old"))
    return _report(findings)


def check_quote(
    quote: Quote,
    now: datetime,
    *,
    max_age: timedelta,
    max_clock_skew: timedelta = timedelta(0),
    max_spread_bps: int | None = None,
) -> QualityReport:
    """Validate a quote and optionally bound its spread in basis points."""
    findings = list(
        check_freshness(
            min(quote.bid_updated_at, quote.ask_updated_at),
            quote.received_at,
            now,
            max_age=max_age,
            max_clock_skew=max_clock_skew,
        ).findings
    )
    if quote.bid >= quote.ask:
        findings.append(_error("crossed_book", "Bid is not below ask"))
    if max_spread_bps is not None:
        if max_spread_bps < 0:
            raise ValueError("max_spread_bps must be non-negative")
        midpoint = (quote.bid + quote.ask) / 2
        spread_bps = (quote.ask - quote.bid) / midpoint * Decimal(10_000)
        if spread_bps > Decimal(max_spread_bps):
            findings.append(_warning("wide_spread", f"Spread is {spread_bps:.2f} bps"))
    return _report(findings)


def check_candle_series(
    candles: Sequence[Candle],
    *,
    interval_minutes: int,
    now: datetime | None = None,
    max_age: timedelta | None = None,
    max_clock_skew: timedelta = timedelta(0),
    allow_gaps: bool = False,
) -> QualityReport:
    """Validate ordering, uniqueness, continuity and freshness of a series.

    Duplicate opens or overlapping intervals are errors; a missing interval is a
    warning unless ``allow_gaps`` is set. Candle-level OHLC validity is already
    enforced by the domain type and is re-checked here for defence in depth.
    """
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be positive")
    if not candles:
        return _report((), missing=True)
    findings: list[Finding] = []
    step = timedelta(minutes=interval_minutes)
    seen: set[datetime] = set()
    previous: Candle | None = None
    for candle in candles:
        if candle.interval_minutes != interval_minutes:
            findings.append(
                _error("mixed_interval", "Candle interval does not match the series")
            )
        if candle.opened_at in seen:
            findings.append(
                _error("duplicate_candle", f"Duplicate open at {candle.opened_at}")
            )
        seen.add(candle.opened_at)
        if previous is not None:
            if candle.opened_at <= previous.opened_at:
                findings.append(_error("out_of_order", "Candles are not increasing"))
            elif candle.opened_at - previous.opened_at > step and not allow_gaps:
                findings.append(
                    _warning(
                        "gap",
                        f"Missing {candle.opened_at - previous.opened_at - step} "
                        "between candles",
                    )
                )
        if not (
            candle.low <= candle.open <= candle.high
            and candle.low <= candle.close <= candle.high
            and candle.high >= candle.low
        ):
            findings.append(_error("invalid_ohlc", "Candle range is inconsistent"))
        previous = candle
    if max_age is not None:
        latest = max(candles, key=lambda c: c.opened_at)
        findings.extend(
            check_freshness(
                latest.opened_at,
                latest.received_at,
                now or datetime.now(UTC),
                max_age=max_age,
                max_clock_skew=max_clock_skew,
            ).findings
        )
    return _report(findings)


def check_order_book(
    bids: Sequence[tuple[Decimal, Decimal]],
    asks: Sequence[tuple[Decimal, Decimal]],
    *,
    received_at: datetime,
    now: datetime,
    max_age: timedelta,
    max_clock_skew: timedelta = timedelta(0),
    max_levels: int = 100,
) -> QualityReport:
    """Validate price/size levels, ordering, crossed/locked books and freshness."""
    if max_levels <= 0:
        raise ValueError("max_levels must be positive")
    findings: list[Finding] = list(
        check_freshness(
            received_at,
            received_at,
            now,
            max_age=max_age,
            max_clock_skew=max_clock_skew,
        ).findings
    )
    if len(bids) > max_levels or len(asks) > max_levels:
        findings.append(_error("too_many_levels", "Order book exceeds the size cap"))
    for side, levels in (("bid", bids), ("ask", asks)):
        previous_price: Decimal | None = None
        for price, size in levels:
            if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
                findings.append(_error("invalid_price", f"Invalid {side} price"))
                continue
            if not isinstance(size, Decimal) or not size.is_finite() or size < 0:
                findings.append(_error("invalid_size", f"Invalid {side} size"))
            if previous_price is not None:
                if side == "bid" and price >= previous_price:
                    findings.append(
                        _error("unsorted_book", "Bids must be strictly descending")
                    )
                if side == "ask" and price <= previous_price:
                    findings.append(
                        _error("unsorted_book", "Asks must be strictly ascending")
                    )
            previous_price = price
    valid_bids = [p for p, _ in bids if isinstance(p, Decimal) and p.is_finite()]
    valid_asks = [p for p, _ in asks if isinstance(p, Decimal) and p.is_finite()]
    if valid_bids and valid_asks and max(valid_bids) >= min(valid_asks):
        findings.append(_error("crossed_book", "Best bid is not below best ask"))
    return _report(findings)
