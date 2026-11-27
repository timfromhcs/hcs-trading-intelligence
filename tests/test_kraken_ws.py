"""Offline tests for the Kraken v2 book feed state machine."""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence

from hcs_trader.data.kraken_ws import FeedEvent, FeedEventKind, KrakenWebSocketFeed
from hcs_trader.domain.markets import MARKET_REGISTRY

MARKET = MARKET_REGISTRY.resolve("BTC/USD").market_id


def _frame(sequence: int, *, bid: str = "100", ask: str = "101") -> str:
    return json.dumps(
        {
            "channel": "book",
            "type": "update",
            "data": {
                "symbol": "BTC/USD",
                "seq": sequence,
                "bids": [[bid, "1", "2026-11-27T12:00:00Z"]],
                "asks": [[ask, "2", "2026-11-27T12:00:00Z"]],
                "checksum": 123,
            },
        }
    )


def _feed(
    streams: Sequence[Sequence[str]],
    *,
    max_attempts: int = 5,
    events: list[FeedEvent] | None = None,
) -> KrakenWebSocketFeed:
    iterator = iter(streams)

    async def connect(cursor: int | None) -> AsyncIterator[str]:
        stream = next(iterator, ())
        for message in stream:
            yield message

    return KrakenWebSocketFeed(
        MARKET,
        connect,
        on_event=(events.append if events is not None else (lambda _event: None)),
        max_attempts=max_attempts,
        base_delay=0.001,
        max_delay=0.002,
    )


def test_clean_stream_emits_quotes_and_advances_cursor() -> None:
    events: list[FeedEvent] = []
    feed = _feed([[_frame(1), _frame(2), _frame(3)]], events=events)
    asyncio.run(feed.run())
    assert [event.kind for event in events] == [FeedEventKind.QUOTE] * 3
    assert feed.cursor == 3
    assert events[0].quote is not None
    assert str(events[0].quote.bid) == "100"


def test_gap_triggers_resubscribe_from_cursor() -> None:
    events: list[FeedEvent] = []
    feed = _feed([[_frame(1), _frame(4)], [_frame(4), _frame(5)]], events=events)
    asyncio.run(feed.run())
    kinds = [event.kind for event in events]
    assert FeedEventKind.GAP in kinds
    assert kinds.index(FeedEventKind.GAP) < kinds.index(FeedEventKind.RESUBSCRIBE)
    assert feed.cursor == 5
    quotes = [event for event in events if event.kind is FeedEventKind.QUOTE]
    assert [event.sequence for event in quotes] == [1, 4, 5]


def test_duplicate_frame_after_reconnect_is_ignored() -> None:
    events: list[FeedEvent] = []
    feed = _feed(
        [[_frame(1), _frame(3)], [_frame(3), _frame(3), _frame(4)]], events=events
    )
    asyncio.run(feed.run())
    quotes = [event for event in events if event.kind is FeedEventKind.QUOTE]
    assert [event.sequence for event in quotes] == [1, 3, 4]


def test_malformed_frame_degrades_without_a_quote() -> None:
    events: list[FeedEvent] = []
    feed = _feed([["{not json}"]], max_attempts=1, events=events)
    asyncio.run(feed.run())
    kinds = [event.kind for event in events]
    assert FeedEventKind.MALFORMED in kinds
    assert kinds[-1] is FeedEventKind.DEGRADED
    assert feed.cursor is None
    assert not any(event.quote is not None for event in events)


def test_crossed_book_is_rejected() -> None:
    events: list[FeedEvent] = []
    feed = _feed([[_frame(1, bid="102", ask="101")]], max_attempts=1, events=events)
    asyncio.run(feed.run())
    assert events[0].kind is FeedEventKind.MALFORMED
    assert feed.cursor is None


def test_heartbeat_and_status_frames_are_ignored() -> None:
    events: list[FeedEvent] = []
    heartbeat = json.dumps({"channel": "heartbeat"})
    status = json.dumps({"method": "subscribe", "success": True})
    feed = _feed([[heartbeat, status, _frame(7)]], events=events)
    asyncio.run(feed.run())
    assert [event.kind for event in events] == [FeedEventKind.QUOTE]
    assert feed.cursor == 7
