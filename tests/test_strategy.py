from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hcs_trader.data.kraken import BTC_USD, SOURCE
from hcs_trader.domain.market_data import Candle, DataQuality
from hcs_trader.strategy import (
    Direction,
    MarketState,
    PortfolioState,
    RsiRegimeStrategy,
    SignalProposal,
    SmaTrendStrategy,
    Strategy,
    is_expired,
    replay_signals,
)

_START = datetime(2020, 1, 1, tzinfo=UTC)
_FLAT = PortfolioState(Decimal(1000), Decimal(0))
_LONG = PortfolioState(Decimal(0), Decimal(1))


def candles(prices: list[int]) -> list[Candle]:
    out = []
    for index, value in enumerate(prices):
        opened = _START + timedelta(hours=index)
        price = Decimal(value)
        out.append(
            Candle(
                BTC_USD,
                SOURCE,
                60,
                opened,
                opened + timedelta(hours=1),
                price,
                price + 1,
                price - 1,
                price,
                Decimal(5),
                1,
            )
        )
    return out


def state(prices: list[int], quality: DataQuality = DataQuality.FRESH) -> MarketState:
    return MarketState(BTC_USD, 60, tuple(candles(prices)), quality)


def test_sma_trend_proposes_long_then_exit_with_evidence_and_expiry() -> None:
    strategy = SmaTrendStrategy(fast=2, slow=4)
    rising = state([10, 11, 12, 13, 14, 15])
    long = strategy.evaluate(rising, _FLAT)
    assert long.direction is Direction.LONG
    assert long.invalidation is not None and long.invalidation > 0
    assert long.evidence_ids and long.data_quality is DataQuality.FRESH
    assert long.created_at == rising.as_of
    assert long.expires_at == long.created_at + timedelta(minutes=60)
    assert not is_expired(long, long.created_at)
    assert is_expired(long, long.expires_at)
    assert strategy.evaluate(rising, _LONG).direction is Direction.NO_TRADE
    falling = state([15, 14, 13, 12, 11, 10])
    assert strategy.evaluate(falling, _LONG).direction is Direction.EXIT
    assert strategy.explain(long).parameters == {"fast": "2", "slow": "4"}


@pytest.mark.parametrize(
    ("quality", "reason"),
    [
        (DataQuality.STALE, "data_quality_stale"),
        (DataQuality.INVALID, "data_quality_invalid"),
    ],
)
def test_bad_data_quality_is_no_trade(quality: DataQuality, reason: str) -> None:
    result = SmaTrendStrategy(2, 4).evaluate(state([2, 3, 4, 5, 6, 7], quality), _FLAT)
    assert result.direction is Direction.NO_TRADE and reason in result.reasons


def test_warmup_gap_and_empty_series_are_no_trade() -> None:
    strategy = SmaTrendStrategy(2, 4)
    assert strategy.warmup_requirements().candles == 4
    assert "warmup_incomplete" in strategy.evaluate(state([2, 3, 4]), _FLAT).reasons
    full = candles([2, 3, 4, 5, 6, 7])
    gapped = MarketState(BTC_USD, 60, tuple(full[:2] + full[4:]), DataQuality.FRESH)
    assert "warmup_incomplete" in strategy.evaluate(gapped, _FLAT).reasons
    empty = MarketState(BTC_USD, 60, (), DataQuality.FRESH)
    assert "no_candles" in strategy.evaluate(empty, _FLAT).reasons


def test_rsi_regime_enters_oversold_dip_in_uptrend_and_exits_overbought() -> None:
    strategy = RsiRegimeStrategy(rsi_period=2, trend_period=8)
    dip = state([10, 30, 50, 70, 90, 110, 130, 150, 140, 130, 125])
    assert strategy.evaluate(dip, _FLAT).direction is Direction.LONG
    rally = state([10, 9, 8, 9, 9, 12, 16, 21])
    assert strategy.evaluate(rally, _LONG).direction is Direction.EXIT
    assert strategy.evaluate(rally, _FLAT).direction is Direction.NO_TRADE
    with pytest.raises(ValueError, match="oversold"):
        RsiRegimeStrategy(oversold=Decimal(80))


def test_strategies_satisfy_protocol_and_are_immutable() -> None:
    for strategy in (SmaTrendStrategy(), RsiRegimeStrategy()):
        typed: Strategy = strategy
        assert typed.strategy_id and typed.version
        with pytest.raises(AttributeError):
            strategy.version = "9"  # type: ignore[misc]


def test_proposal_validation() -> None:
    base = SmaTrendStrategy(2, 4).evaluate(state([10, 11, 12, 13, 14]), _FLAT)
    with pytest.raises(ValueError, match="expire"):
        replace(base, expires_at=base.created_at)
    with pytest.raises(ValueError, match="confidence"):
        replace(base, confidence=Decimal(2))
    with pytest.raises(ValueError, match="invalidation"):
        replace(base, invalidation=None)
    with pytest.raises(ValueError, match="UTC"):
        replace(base, created_at=datetime(2020, 1, 1))
    with pytest.raises(ValueError, match="NO_TRADE"):
        replace(base, direction=Direction.NO_TRADE, reasons=())


@given(st.lists(st.integers(min_value=2, max_value=500), min_size=0, max_size=60))
def test_property_replay_has_no_lookahead_and_valid_proposals(
    prices: list[int],
) -> None:
    strategy = SmaTrendStrategy(2, 5)
    series = candles(prices)
    proposals = list(replay_signals(strategy, series, _FLAT))
    assert len(proposals) == max(0, len(series) - 4)
    for offset, proposal in enumerate(proposals):
        assert isinstance(proposal, SignalProposal)
        end = 5 + offset
        # Truncating the future must not change a proposal formed at `end`.
        again = list(replay_signals(strategy, series[:end], _FLAT))[offset]
        assert again == proposal
        assert proposal.created_at == series[end - 1].closed_at
