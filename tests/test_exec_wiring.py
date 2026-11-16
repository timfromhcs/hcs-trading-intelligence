"""Execution wiring: persisted risk verdicts, kill-switch auto-triggers, dry run."""

import base64
import hashlib
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest

from hcs_trader.data.kraken import BTC_USD, SOURCE
from hcs_trader.domain.execution import SpotOrder, SpotOrderType, SpotSide
from hcs_trader.domain.lifecycle import OrderState
from hcs_trader.domain.market_data import Quote
from hcs_trader.kraken_spot import KrakenAmbiguousExecutionError, KrakenSpotAdapter
from hcs_trader.promotion import PromotionStage, StrategyCandidate
from hcs_trader.risk import RiskEngine, RiskPolicy, RiskRejected, policy_version
from hcs_trader.spot_execution import KrakenSpotExecution
from hcs_trader.storage import AuditStore, KillSwitchCause, RiskOutcome

_KEY = "wiring-test-key"  # synthetic
_SECRET = base64.b64encode(b"01234567890123456789012345678901").decode("ascii")
_TXID = "OWIRE-AAAAA-BBBBB"


def _order(client_id: str = "wire-1", quantity: str = "0.005") -> SpotOrder:
    now = datetime.now(UTC)
    return SpotOrder(
        client_order_id=client_id,
        order_id="ord-" + client_id,
        venue="kraken",
        market="BTC/USD",
        side=SpotSide.BUY,
        order_type=SpotOrderType.LIMIT,
        quantity=Decimal(quantity),
        limit_price=Decimal("50000.00"),
        state=OrderState.PREVIEWED,
        risk_decision_id="risk-placeholder",
        created_at=now,
        updated_at=now,
    )


def _quote() -> Quote:
    now = datetime.now(UTC)
    return Quote(BTC_USD, SOURCE, Decimal("49999"), Decimal("50000"), now, now, now)


def _store(tmp_path: Path, *, canary: bool = True) -> tuple[AuditStore, str]:
    store = AuditStore(tmp_path / "wiring.db")
    store.initialize()
    candidate = StrategyCandidate(
        candidate_id="wire-cand",
        name="SMA",
        version="v1.0.0",
        dataset_hash="snapshot",
        stage=PromotionStage.PAPER_APPROVED,
    )
    if canary:
        now = datetime.now(UTC)
        store.save_strategy_candidate(candidate)
        store.authorize_spot_canary(
            candidate,
            hashlib.sha256(_KEY.encode()).hexdigest(),
            Decimal("500"),
            now,
            now + timedelta(hours=1),
        )
        assert store.update_candidate_stage(candidate, PromotionStage.LIVE_CANARY, now)
    return store, candidate.candidate_id


Handler = Callable[[httpx2.Request], httpx2.Response]


def _venue(calls: list[str], *, add_order: Handler | None = None) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        endpoint = request.url.path.rsplit("/", 1)[1]
        calls.append(endpoint)
        if endpoint == "AddOrder":
            if add_order is not None:
                return add_order(request)
            return httpx2.Response(
                200,
                json={
                    "error": [],
                    "result": {"descr": {"order": "buy"}, "txid": [_TXID]},
                },
            )
        if endpoint == "CancelOrder":
            return httpx2.Response(200, json={"error": [], "result": {"count": 1}})
        return httpx2.Response(200, json={"error": [], "result": {}})

    return handler


def _risk(**policy: object) -> RiskEngine:
    return RiskEngine(RiskPolicy(live_mode_enabled=True, **policy))  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_allowed_and_rejected_verdicts_are_persisted(tmp_path: Path) -> None:
    store, candidate = _store(tmp_path)
    calls: list[str] = []
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(_venue(calls))) as c:
        spot = KrakenSpotExecution(store, KrakenSpotAdapter(_KEY, _SECRET, c), _risk())
        done = await spot.submit(
            _order(), _quote(), candidate_id=candidate, strategy_version="v1.0.0"
        )
        stored = store.get_risk_decision(done.risk_decision_id)
        assert stored is not None and stored.outcome is RiskOutcome.ALLOWED
        assert stored.policy_version == policy_version(_risk().policy)

        with pytest.raises(RiskRejected, match="not approved"):
            await spot.submit(
                _order("wire-2"),
                _quote(),
                candidate_id=candidate,
                strategy_version="v9",
            )
    newest = store.list_risk_decisions(1)[0]
    assert newest.outcome is RiskOutcome.REJECTED and not newest.allowed


@pytest.mark.anyio
async def test_repeated_rejects_trip_the_kill_switch(tmp_path: Path) -> None:
    store, candidate = _store(tmp_path, canary=False)
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(_venue([]))) as client:
        risk = RiskEngine(RiskPolicy(max_consecutive_rejects=3))  # live mode off
        spot = KrakenSpotExecution(
            store, KrakenSpotAdapter(_KEY, _SECRET, client), risk
        )
        for index in range(3):
            assert not store.kill_switch_state().active
            with pytest.raises(RiskRejected):
                await spot.submit(
                    _order(f"rej-{index}"),
                    _quote(),
                    candidate_id=candidate,
                    strategy_version="v1.0.0",
                )
    state = store.kill_switch_state()
    assert state.active and state.cause is KillSwitchCause.REPEATED_REJECTS
    assert store.execution_pause().paused


@pytest.mark.anyio
async def test_persisted_pause_blocks_before_any_venue_call(tmp_path: Path) -> None:
    store, candidate = _store(tmp_path)
    calls: list[str] = []
    store.set_execution_pause(True)
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(_venue(calls))) as c:
        spot = KrakenSpotExecution(store, KrakenSpotAdapter(_KEY, _SECRET, c), _risk())
        with pytest.raises(RiskRejected, match="paused"):
            await spot.submit(
                _order(), _quote(), candidate_id=candidate, strategy_version="v1.0.0"
            )
    assert calls == []
    assert store.list_risk_decisions(1)[0].outcome is RiskOutcome.REJECTED


@pytest.mark.anyio
async def test_ambiguous_submit_trips_reconciliation_required(tmp_path: Path) -> None:
    store, candidate = _store(tmp_path)

    def timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectTimeout("boom", request=request)

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(_venue([], add_order=timeout))
    ) as client:
        spot = KrakenSpotExecution(
            store, KrakenSpotAdapter(_KEY, _SECRET, client), _risk()
        )
        observed = await spot.submit(
            _order(), _quote(), candidate_id=candidate, strategy_version="v1.0.0"
        )
    assert observed.state is OrderState.RECONCILIATION_REQUIRED
    state = store.kill_switch_state()
    assert state.active and state.cause is KillSwitchCause.RECONCILIATION_REQUIRED


@pytest.mark.anyio
async def test_balance_mismatch_trips_and_cancels_open_orders(tmp_path: Path) -> None:
    store, candidate = _store(tmp_path)
    calls: list[str] = []
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(_venue(calls))) as c:
        spot = KrakenSpotExecution(store, KrakenSpotAdapter(_KEY, _SECRET, c), _risk())
        placed = await spot.submit(
            _order(), _quote(), candidate_id=candidate, strategy_version="v1.0.0"
        )
        assert placed.state is OrderState.ACKNOWLEDGED
        assert await spot.check_balance(
            "BTC", Decimal("1"), Decimal("1.0000001"), Decimal("0.001")
        )
        assert not store.kill_switch_state().active and "CancelOrder" not in calls

        assert not await spot.check_balance("BTC", Decimal("1"), Decimal("0.9"))
    state = store.kill_switch_state()
    assert state.active and state.cause is KillSwitchCause.UNEXPLAINED_BALANCE_DELTA
    assert "CancelOrder" in calls
    stored = store.get_spot_order(placed.client_order_id)
    assert stored is not None and stored.state is OrderState.CANCELLED


@pytest.mark.anyio
async def test_cancel_failure_is_best_effort(tmp_path: Path) -> None:
    store, candidate = _store(tmp_path)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("CancelOrder"):
            return httpx2.Response(500)
        return _venue([])(request)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        spot = KrakenSpotExecution(
            store, KrakenSpotAdapter(_KEY, _SECRET, client), _risk()
        )
        placed = await spot.submit(
            _order(), _quote(), candidate_id=candidate, strategy_version="v1.0.0"
        )
        await spot.trip(KillSwitchCause.MANUAL, "operator drill")
        assert await spot.cancel_open_orders() == []
    assert store.kill_switch_state().active
    stored = store.get_spot_order(placed.client_order_id)
    assert stored is not None and stored.state is OrderState.ACKNOWLEDGED


@pytest.mark.anyio
async def test_startup_reconcile_of_unconfirmed_order_trips(tmp_path: Path) -> None:
    store, candidate = _store(tmp_path)
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(_venue([]))) as client:
        spot = KrakenSpotExecution(
            store, KrakenSpotAdapter(_KEY, _SECRET, client), _risk()
        )
        await spot.submit(
            _order(), _quote(), candidate_id=candidate, strategy_version="v1.0.0"
        )
        # QueryOrders returns an empty result: venue identity is unconfirmed.
        results = await spot.reconcile_startup()
    assert results and results[0].state is OrderState.RECONCILIATION_REQUIRED
    state = store.kill_switch_state()
    assert state.active and state.cause is KillSwitchCause.RECONCILIATION_REQUIRED


def test_risk_engine_reads_persisted_pause_and_fails_closed(tmp_path: Path) -> None:
    store, _ = _store(tmp_path, canary=False)
    quote = _quote()
    order = _order()
    now = datetime.now(UTC)
    quality = quote.quality_at(
        now, max_age=timedelta(seconds=30), max_clock_skew=timedelta(seconds=2)
    )

    def verdict(engine: RiskEngine) -> bool:
        return engine.evaluate_order(order, quote, quality, 0, "v1.0.0", now).allowed

    engine = RiskEngine(RiskPolicy(live_mode_enabled=True), store.fail_safe_paused)
    assert verdict(engine)
    store.set_execution_pause(True)
    assert not verdict(engine)

    def broken() -> bool:
        raise OSError("unreadable")

    assert not verdict(RiskEngine(RiskPolicy(live_mode_enabled=True), broken))


def test_policy_version_is_stable_and_policy_sensitive() -> None:
    assert policy_version(RiskPolicy()) == policy_version(RiskPolicy())
    assert policy_version(RiskPolicy()) != policy_version(
        RiskPolicy(max_notional_usd=Decimal("999"))
    )
    assert policy_version(RiskPolicy()).startswith("policy-")


@pytest.mark.anyio
async def test_dry_run_validates_without_funds_or_stored_orders(tmp_path: Path) -> None:
    store, _ = _store(tmp_path, canary=False)
    seen: list[dict[str, list[str]]] = []

    def validate(request: httpx2.Request) -> httpx2.Response:
        seen.append(urllib.parse.parse_qs(request.content.decode()))
        return httpx2.Response(
            200,
            json={"error": [], "result": {"descr": {"order": "buy 0.005 XBTUSD"}}},
        )

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(_venue([], add_order=validate))
    ) as client:
        adapter = KrakenSpotAdapter(_KEY, _SECRET, client, validate_only=True)
        # Policy live mode is OFF: a dry run must not need or enable it.
        spot = KrakenSpotExecution(store, adapter, RiskEngine(RiskPolicy()))
        result = await spot.dry_run(_order(), _quote(), strategy_version="v1.0.0")
        assert result.validated and result.market == "BTC/USD"
        assert seen[0]["validate"] == ["true"]
        assert store.list_open_spot_orders("kraken") == []
        assert store.get_spot_order("wire-1") is None
        assert store.list_risk_decisions() == []

        with pytest.raises(RiskRejected, match="not approved"):
            await spot.dry_run(_order(), _quote(), strategy_version="v9")

        store.set_execution_pause(True)
        with pytest.raises(RiskRejected, match="paused"):
            await spot.dry_run(_order(), _quote(), strategy_version="v1.0.0")
        assert len(seen) == 1  # rejected dry runs never reach the venue


@pytest.mark.anyio
async def test_dry_run_requires_validation_adapter_and_rejects_txid(
    tmp_path: Path,
) -> None:
    store, candidate = _store(tmp_path, canary=False)

    def with_txid(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "error": [],
                "result": {"descr": {"order": "buy"}, "txid": ["unexpected"]},
            },
        )

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(_venue([], add_order=with_txid))
    ) as client:
        live = KrakenSpotExecution(
            store, KrakenSpotAdapter(_KEY, _SECRET, client), _risk()
        )
        with pytest.raises(RiskRejected, match="validation-only"):
            await live.dry_run(_order(), _quote(), strategy_version="v1.0.0")
        dry = KrakenSpotExecution(
            store,
            KrakenSpotAdapter(_KEY, _SECRET, client, validate_only=True),
            _risk(),
        )
        with pytest.raises(RiskRejected, match="validation-only"):
            await dry.submit(
                _order(), _quote(), candidate_id=candidate, strategy_version="v1.0.0"
            )
        with pytest.raises(KrakenAmbiguousExecutionError):
            await dry.dry_run(_order(), _quote(), strategy_version="v1.0.0")
