"""Kraken WebSocket v2 book feed with a resumable cursor.

The network transport is injected so the state machine is testable offline and
no websocket dependency is required by the distributable. A gap in the
sequence stream, a malformed message or a dropped connection never yields a
quote: the feed reconnects from the last committed sequence with bounded
exponential backoff and jitter, then gives up in DEGRADED state.
"""

import asyncio
import json
import random
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from hcs_trader.domain.assets import MarketId
from hcs_trader.domain.market_data import Quote

Connect = Callable[[int | None], AsyncIterator[str]]
_SOURCE = "kraken-ws-v2"
_MAX_MESSAGE_BYTES = 65_536


class FeedEventKind(StrEnum):
    QUOTE = "quote"
    GAP = "gap"
    MALFORMED = "malformed"
    DISCONNECT = "disconnect"
    BACKOFF = "backoff"
    RESUBSCRIBE = "resubscribe"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class FeedEvent:
    kind: FeedEventKind
    detail: str
    quote: Quote | None = None
    sequence: int | None = None


class _FeedDisconnect(Exception):
    pass


def _decimal(value: object) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("Book level is not a string")
    amount = Decimal(value)
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Book level must be a positive finite Decimal")
    return amount


def _levels(raw: object) -> tuple[tuple[Decimal, Decimal], ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("Book side must be a non-empty list")
    result: list[tuple[Decimal, Decimal]] = []
    for level in raw:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            raise ValueError("Book level has an unexpected shape")
        result.append((_decimal(level[0]), _decimal(level[1])))
    return tuple(result)


class KrakenWebSocketFeed:
    """Consume a synthetic or real book stream and emit normalized quotes."""

    def __init__(
        self,
        market: MarketId,
        connect: Connect,
        *,
        on_event: Callable[[FeedEvent], None],
        max_attempts: int = 5,
        base_delay: float = 0.01,
        max_delay: float = 0.2,
        rng: random.Random | None = None,
    ) -> None:
        if max_attempts < 1 or base_delay <= 0 or max_delay < base_delay:
            raise ValueError("Invalid backoff configuration")
        self._market = market
        self._connect = connect
        self._on_event = on_event
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
        # Jitter only spreads reconnects; it is not used for any security purpose.
        self._rng = rng or random.Random(0)  # noqa: S311
        self._cursor: int | None = None

    @property
    def cursor(self) -> int | None:
        return self._cursor

    def _emit(
        self,
        kind: FeedEventKind,
        detail: str,
        *,
        quote: Quote | None = None,
        sequence: int | None = None,
    ) -> None:
        self._on_event(FeedEvent(kind, detail, quote, sequence))

    async def _consume_once(self, *, resubscribed: bool = False) -> None:
        previous = self._cursor
        awaiting_snapshot = resubscribed
        async for raw in self._connect(previous):
            try:
                frame = _parse_frame(raw, self._market)
            except (ValueError, json.JSONDecodeError) as error:
                self._emit(FeedEventKind.MALFORMED, str(error)[:160])
                raise _FeedDisconnect("malformed frame") from error
            if frame is None or frame.quote is None:
                continue
            if frame.sequence is None:
                continue
            if awaiting_snapshot:
                # A fresh subscription starts with a snapshot, which replaces the
                # earlier sequence baseline; the gap was already reported.
                previous = None
                awaiting_snapshot = False
            if previous is not None:
                if frame.sequence <= previous:
                    continue  # duplicate or replayed frame after reconnect
                if frame.sequence > previous + 1:
                    self._emit(
                        FeedEventKind.GAP,
                        f"Missed sequences {previous + 1}..{frame.sequence - 1}",
                        sequence=frame.sequence,
                    )
                    raise _FeedDisconnect("sequence gap")
            self._cursor = previous = frame.sequence
            self._emit(
                FeedEventKind.QUOTE,
                "book update",
                quote=frame.quote,
                sequence=frame.sequence,
            )

    async def run(self) -> None:
        attempt = 0
        resubscribed = False
        while True:
            try:
                await self._consume_once(resubscribed=resubscribed)
                return  # the stream ended without an error
            except asyncio.CancelledError:
                raise
            except (_FeedDisconnect, OSError) as error:
                self._emit(FeedEventKind.DISCONNECT, str(error))
                attempt += 1
                if attempt >= self._max_attempts:
                    self._emit(
                        FeedEventKind.DEGRADED,
                        f"Giving up after {attempt} reconnect attempts",
                    )
                    return
                delay = min(self._max_delay, self._base_delay * 2 ** (attempt - 1)) * (
                    0.5 + self._rng.random() / 2
                )
                self._emit(
                    FeedEventKind.BACKOFF,
                    f"Waiting {delay:.3f}s",
                    sequence=self._cursor,
                )
                await asyncio.sleep(delay)
                self._emit(
                    FeedEventKind.RESUBSCRIBE,
                    "Resubscribing from cursor",
                    sequence=self._cursor,
                )
                resubscribed = True


def _parse_frame(raw: str, market: MarketId) -> FeedEvent | None:
    """Parse a book frame into a quote event with the venue sequence."""
    if len(raw.encode("utf-8")) > _MAX_MESSAGE_BYTES:
        raise ValueError("WebSocket frame exceeds the size cap")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("WebSocket frame is not an object")
    if payload.get("error") or payload.get("success") is False:
        raise ValueError("Venue returned an error frame")
    if payload.get("channel") != "book" or not isinstance(payload.get("data"), dict):
        return None
    data = payload["data"]
    sequence = data.get("seq")
    if type(sequence) is not int or sequence < 0:
        raise ValueError("Book update has no valid sequence")
    bids = _levels(data.get("bids"))
    asks = _levels(data.get("asks"))
    if bids[0][0] >= asks[0][0]:
        raise ValueError("Book is crossed or locked")
    received_at = datetime.now(UTC)
    observed_at = received_at
    stamp = data.get("timestamp")
    if isinstance(stamp, str):
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if parsed.utcoffset() is not None and parsed <= received_at + timedelta(
                seconds=2
            ):
                observed_at = parsed.astimezone(UTC)
        except ValueError:
            pass
    quote = Quote(
        market, _SOURCE, bids[0][0], asks[0][0], observed_at, observed_at, received_at
    )
    return FeedEvent(
        kind=FeedEventKind.QUOTE,
        detail="book update",
        quote=quote,
        sequence=sequence,
    )
