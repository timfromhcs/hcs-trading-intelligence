"""Use cases shared by REST and MCP.

Transports validate input and map ``ApplicationError`` subclasses to their own
error model. Every side effect (paper orders, pause, candidate creation,
backtest persistence) happens here, so both entry points follow one policy path.
"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from hcs_trader.application.capabilities import (
    ChainCapability,
    chain_capability_matrix,
)
from hcs_trader.backtest import BacktestConfig, BacktestResult, run_backtest
from hcs_trader.data import ecb, fred
from hcs_trader.data.feed_status import IngestRecord
from hcs_trader.data.kraken import BTC_USD, CANDLE_INTERVALS, SOURCE
from hcs_trader.data.source_status import DataSourcesReport, build_source_status
from hcs_trader.data.sources import DEFAULT_REGISTRY
from hcs_trader.domain.macro import MacroObservation
from hcs_trader.domain.market_data import Candle, DataQuality, Quote
from hcs_trader.domain.news import NewsHit, NewsItem
from hcs_trader.domain.paper import (
    PaperBalance,
    PaperConflict,
    PaperFill,
    PaperOrder,
    PaperRejected,
    PaperSide,
    book_fill,
    simulated_terms,
)
from hcs_trader.domain.wallet import WalletSnapshot
from hcs_trader.indicators import Indicators, calculate_indicators
from hcs_trader.paper_execution import PaperExecution
from hcs_trader.promotion import (
    PromotionRejected,
    PromotionStage,
    ReportPayload,
    StrategyCandidate,
    get_latest_shadow_run,
)
from hcs_trader.risk import RiskPolicy
from hcs_trader.storage import AuditStore, ExecutionPause
from hcs_trader.walk_forward import WalkForwardConfig, WalkForwardResult, walk_forward

_WALLET_ID = re.compile(r"[A-Za-z0-9]{8,128}")
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_MAX_QUOTE_AGE = timedelta(seconds=30)
_MAX_CLOCK_SKEW = timedelta(seconds=2)
_LOCAL_ERRORS = (sqlite3.Error, ValidationError, ValueError)


class ApplicationError(Exception):
    """Base class for use-case failures that transports map to their own codes."""


class InvalidRequest(ApplicationError):
    """The request is well-formed but not acceptable."""


class NotFound(ApplicationError):
    """The requested resource does not exist."""


class Forbidden(ApplicationError):
    """The capability is disabled in this process."""


class Rejected(ApplicationError):
    """A deterministic rule rejected the request (conflict)."""


class Unavailable(ApplicationError):
    """Local storage or a required dependency is unavailable."""


class AssetInfo(BaseModel):
    asset_id: str
    symbol: str
    kind: str
    role: str


class OrderBookLevel(BaseModel):
    price: Decimal


class OrderBookSnapshot(BaseModel):
    market: str
    source: str
    quality: DataQuality
    depth_levels: int
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    spread: Decimal | None
    received_at: datetime | None
    note: str


class Position(BaseModel):
    asset: str
    quantity: Decimal
    cost_basis_usd: Decimal
    realized_pnl_usd: Decimal
    fees_usd: Decimal
    mode: str = "PAPER"


class WalletBalanceView(BaseModel):
    address: str
    asset: str
    base_units: str
    decimals: int
    observed_at: datetime
    block_number: int | None = None
    slot: int | None = None


class WalletTransactions(BaseModel):
    wallet_id: str
    available: bool
    transactions: list[dict[str, Any]]
    note: str


class OrderPreview(BaseModel):
    mode: str = "PAPER"
    would_fill: bool
    reasons: list[str]
    side: str
    quantity: Decimal
    price: Decimal | None
    fee: Decimal | None
    notional: Decimal | None
    cash_after: Decimal | None
    quantity_after: Decimal | None
    quote_quality: DataQuality | None
    execution_paused: bool
    duplicate_of_existing_fill: bool
    note: str = "Preview only: no order was created and no ledger entry was written."


class StrategyMetrics(BaseModel):
    candidate: StrategyCandidate
    shadow_run_id: str | None
    net_return: Decimal | None
    max_drawdown: Decimal | None
    fills: int | None
    note: str


class BacktestRun(BaseModel):
    report_id: str
    persisted: bool
    result: BacktestResult | WalkForwardResult


@dataclass(frozen=True, slots=True)
class OrderRecord:
    kind: str
    payload: dict[str, Any]


def spot_order_dict(order: Any) -> dict[str, Any]:
    """Serialise a SpotOrder with decimal strings (single shared mapping)."""

    def opt(value: Any) -> str | None:
        return None if value is None else str(value)

    return {
        "client_order_id": order.client_order_id,
        "order_id": order.order_id,
        "venue": order.venue,
        "market": order.market,
        "side": order.side.value,
        "order_type": order.order_type.value,
        "quantity": str(order.quantity),
        "limit_price": opt(order.limit_price),
        "state": order.state.value,
        "risk_decision_id": order.risk_decision_id,
        "created_at": order.created_at.isoformat(),
        "updated_at": order.updated_at.isoformat(),
        "venue_order_id": order.venue_order_id,
        "filled_quantity": opt(order.filled_quantity),
        "average_price": opt(order.average_price),
        "fee": opt(order.fee),
    }


def snapshot_view(snapshot: WalletSnapshot) -> WalletBalanceView:
    return WalletBalanceView(
        address=snapshot.address,
        asset=snapshot.asset,
        base_units=snapshot.base_units,
        decimals=snapshot.decimals,
        observed_at=snapshot.observed_at,
        block_number=getattr(snapshot, "block_number", None),
        slot=getattr(snapshot, "slot", None),
    )


class ApplicationService:
    def __init__(
        self,
        store: AuditStore,
        *,
        paper: PaperExecution | None = None,
        policy: RiskPolicy | None = None,
    ) -> None:
        self.store = store
        self.paper = paper
        self.policy = policy or RiskPolicy()

    # --- system -----------------------------------------------------------

    def health(self) -> dict[str, str]:
        try:
            self.store.latest_event()
        except sqlite3.Error:
            raise Unavailable("Audit storage unavailable") from None
        return {"status": "ok"}

    def chain_capabilities(self) -> list[ChainCapability]:
        return chain_capability_matrix()

    def execution_pause(self) -> ExecutionPause:
        try:
            return self.store.execution_pause()
        except (sqlite3.Error, ValueError):
            raise Unavailable("Execution pause state unavailable") from None

    def pause_execution(self) -> ExecutionPause:
        """Set the durable pause. Clearing it is a local operator action only."""
        try:
            return self.store.set_execution_pause(True)
        except (sqlite3.Error, ValueError):
            raise Unavailable("Risk control unavailable") from None

    def resume_execution(self) -> ExecutionPause:
        try:
            return self.store.set_execution_pause(False)
        except (sqlite3.Error, ValueError):
            raise Unavailable("Risk control unavailable") from None

    def risk_policy_summary(self) -> dict[str, Any]:
        policy = self.policy
        pause = self.execution_pause()
        return {
            "kill_switch_paused": pause.paused,
            "live_mode_enabled": policy.live_mode_enabled,
            "allowed_venues": sorted(policy.allowed_venues),
            "allowed_markets": sorted(policy.allowed_markets),
            "min_notional_usd": str(policy.min_notional_usd),
            "max_notional_usd": str(policy.max_notional_usd),
            "max_spread_bps": str(policy.max_spread_bps),
            "max_slippage_bps": str(policy.max_slippage_bps),
            "max_open_orders": policy.max_open_orders,
            "requires_fresh_quote": True,
            "leverage_allowed": False,
            "margin_allowed": False,
            "borrowing_allowed": False,
        }

    # --- market -----------------------------------------------------------

    def list_assets(self) -> list[AssetInfo]:
        assets = [
            AssetInfo(
                asset_id=f"{row.signing_namespace}:{row.symbol}",
                symbol=row.symbol,
                kind="native",
                role="traded-or-watched",
            )
            for row in chain_capability_matrix()
        ]
        assets.append(
            AssetInfo(
                asset_id="fiat:iso4217/currency:USD",
                symbol="USD",
                kind="fiat-reporting",
                role="quote",
            )
        )
        return assets

    def latest_quote(self) -> Quote | None:
        try:
            quotes = self.store.quote_history(BTC_USD, limit=1)
        except _LOCAL_ERRORS:
            raise Unavailable("Quote storage unavailable") from None
        return quotes[0] if quotes else None

    def orderbook_snapshot(self) -> OrderBookSnapshot:
        quote = self.latest_quote()
        note = (
            "Top of book only: the archived feed stores the best bid and ask, "
            "not deeper levels."
        )
        if quote is None:
            return OrderBookSnapshot(
                market="BTC/USD",
                source=SOURCE,
                quality=DataQuality.MISSING,
                depth_levels=0,
                bids=[],
                asks=[],
                spread=None,
                received_at=None,
                note=note,
            )
        quality = quote.quality_at(
            datetime.now(UTC), max_age=_MAX_QUOTE_AGE, max_clock_skew=_MAX_CLOCK_SKEW
        )
        return OrderBookSnapshot(
            market="BTC/USD",
            source=quote.source,
            quality=quality,
            depth_levels=1,
            bids=[OrderBookLevel(price=quote.bid)],
            asks=[OrderBookLevel(price=quote.ask)],
            spread=quote.ask - quote.bid,
            received_at=quote.received_at,
            note=note,
        )

    def quote_history(self, limit: int) -> list[Quote]:
        try:
            return self.store.quote_history(BTC_USD, limit=limit)
        except _LOCAL_ERRORS:
            raise Unavailable("Quote storage unavailable") from None

    def candles(self, interval: int, limit: int) -> list[Candle]:
        if interval not in CANDLE_INTERVALS:
            raise InvalidRequest("Unsupported candle interval")
        try:
            return self.store.candle_history(BTC_USD, SOURCE, interval, limit=limit)
        except sqlite3.Error:
            raise Unavailable("Candle storage unavailable") from None

    def indicators(self, interval: int = 60) -> Indicators:
        history = self.candles(interval, 720)
        try:
            return calculate_indicators(history)
        except ValueError:
            raise Unavailable("Local data unavailable") from None

    # --- data sources -----------------------------------------------------

    def data_sources(self) -> DataSourcesReport:
        """Freshness, breaker state and retry timing per registered source."""
        try:
            records = {r.source_id: r for r in self.store.list_ingest_state()}
        except sqlite3.Error:
            raise Unavailable("Source status storage unavailable") from None
        now = datetime.now(UTC)
        return DataSourcesReport(
            generated_at=now,
            sources=[
                build_source_status(
                    entry,
                    records.get(entry.source_id, IngestRecord(entry.source_id)),
                    now,
                )
                for entry in DEFAULT_REGISTRY.all()
            ],
        )

    # --- news / macro -----------------------------------------------------

    def news(self, limit: int) -> list[NewsItem]:
        try:
            return self.store.news(limit=limit)
        except sqlite3.Error:
            raise Unavailable("News storage unavailable") from None

    def search_news(self, query: str, limit: int) -> list[NewsHit]:
        needle = query.strip()
        if not needle or len(needle) > 100:
            raise InvalidRequest("Search text must be 1 to 100 characters")
        try:
            return self.store.search_news(needle, limit=limit)
        except sqlite3.Error:
            raise Unavailable("News storage unavailable") from None

    def news_timeline(self, limit: int) -> list[NewsItem]:
        return sorted(self.news(limit), key=lambda item: item.published_at)

    def macro_series(
        self, source: Literal["ecb", "fred"], limit: int
    ) -> list[MacroObservation]:
        provider = ecb if source == "ecb" else fred
        try:
            return self.store.macro_history(
                provider.SOURCE, provider.SERIES, limit=limit
            )
        except sqlite3.Error:
            raise Unavailable("Macro storage unavailable") from None

    # --- portfolio / wallets ---------------------------------------------

    def paper_balance(self) -> PaperBalance:
        try:
            return self.store.paper_balance()
        except _LOCAL_ERRORS:
            raise Unavailable("Paper ledger unavailable") from None

    def positions(self) -> list[Position]:
        balance = self.paper_balance()
        if balance.quantity <= 0:
            return []
        return [
            Position(
                asset="BTC",
                quantity=balance.quantity,
                cost_basis_usd=balance.cost_basis,
                realized_pnl_usd=balance.realized_pnl,
                fees_usd=balance.fees,
            )
        ]

    def wallet_snapshots(self, limit: int) -> list[WalletSnapshot]:
        try:
            return self.store.wallet_snapshots(limit=limit)
        except _LOCAL_ERRORS:
            raise Unavailable("Wallet snapshot storage unavailable") from None

    def wallet_addresses(self) -> list[str]:
        return sorted({s.address for s in self.wallet_snapshots(100)})

    def _wallet_rows(self, wallet_id: str) -> list[WalletSnapshot]:
        if not _WALLET_ID.fullmatch(wallet_id):
            raise InvalidRequest("Invalid wallet identifier")
        rows = [s for s in self.wallet_snapshots(100) if s.address == wallet_id]
        if not rows:
            raise NotFound("Wallet not found")
        return rows

    def wallet_balances(self, wallet_id: str) -> list[WalletBalanceView]:
        return [snapshot_view(row) for row in self._wallet_rows(wallet_id)]

    def wallet_transactions(self, wallet_id: str) -> WalletTransactions:
        self._wallet_rows(wallet_id)
        return WalletTransactions(
            wallet_id=wallet_id,
            available=False,
            transactions=[],
            note="Transaction history is not stored locally; balances only.",
        )

    # --- strategies -------------------------------------------------------

    def strategies(
        self, stage: PromotionStage | None = None
    ) -> list[StrategyCandidate]:
        try:
            return self.store.list_strategy_candidates(stage=stage)
        except _LOCAL_ERRORS:
            raise Unavailable("Strategy candidate storage unavailable") from None

    def strategy_metrics(self, strategy_id: str) -> StrategyMetrics:
        if not _ID.fullmatch(strategy_id):
            raise InvalidRequest("Invalid strategy identifier")
        try:
            candidate = self.store.get_strategy_candidate(strategy_id)
        except _LOCAL_ERRORS:
            raise Unavailable("Strategy candidate storage unavailable") from None
        if candidate is None:
            raise NotFound("Strategy candidate not found")
        try:
            run_id, report = get_latest_shadow_run(self.store, strategy_id)
        except PromotionRejected:
            return StrategyMetrics(
                candidate=candidate,
                shadow_run_id=None,
                net_return=None,
                max_drawdown=None,
                fills=None,
                note="No shadow run recorded. Backtest results are educational.",
            )
        except _LOCAL_ERRORS:
            raise Unavailable("Paper shadow run unavailable") from None
        return StrategyMetrics(
            candidate=candidate,
            shadow_run_id=run_id,
            net_return=report.net_return,
            max_drawdown=report.max_drawdown,
            fills=len(report.fills),
            note="Simulated net-of-cost result, not an expected live return.",
        )

    def create_candidate(
        self,
        name: str,
        version: str,
        parameters: dict[str, Any],
        dataset_hash: str,
        *,
        candidate_id: str | None = None,
    ) -> StrategyCandidate:
        """Register a RESEARCH-stage candidate.

        With an explicit ``candidate_id`` the call is idempotent: the same
        intent returns the stored candidate, a different intent is rejected.
        """
        now = datetime.now(UTC)
        identifier = (
            candidate_id
            or sha256(
                f"{now.isoformat()}{name}{version}{id(parameters)}".encode()
            ).hexdigest()[:32]
        )
        if candidate_id is not None:
            try:
                existing = self.store.get_strategy_candidate(identifier)
            except _LOCAL_ERRORS:
                raise Unavailable("Strategy candidate storage unavailable") from None
            if existing is not None:
                if (existing.name, existing.version, existing.dataset_hash) != (
                    name,
                    version,
                    dataset_hash,
                ):
                    raise Rejected("Idempotency key reused with a different candidate")
                return existing
        try:
            candidate = StrategyCandidate(
                candidate_id=identifier,
                name=name,
                version=version,
                stage=PromotionStage.RESEARCH,
                parameters=parameters,
                dataset_hash=dataset_hash,
                created_at=now,
                updated_at=now,
            )
        except ValidationError as error:
            raise InvalidRequest("Invalid strategy candidate") from error
        try:
            self.store.save_strategy_candidate(candidate)
        except sqlite3.Error:
            raise Unavailable("Failed to save strategy candidate") from None
        return candidate

    # --- research ---------------------------------------------------------

    def _save_report(
        self, result: BacktestResult | WalkForwardResult, persist: bool
    ) -> BacktestRun:
        payload = ReportPayload(report=result).model_dump_json()
        report_id = sha256(payload.encode("utf-8")).hexdigest()
        if persist:
            try:
                self.store.record_backtest(report_id, payload)
            except sqlite3.Error:
                raise Unavailable("Backtest storage unavailable") from None
        return BacktestRun(report_id=report_id, persisted=persist, result=result)

    def run_backtest(
        self,
        config: BacktestConfig,
        *,
        interval: int = 60,
        limit: int = 720,
        holdout_candles: int = 0,
        persist: bool = True,
    ) -> BacktestRun:
        candles = self.candles(interval, limit)
        try:
            development = candles[:-holdout_candles] if holdout_candles else candles
            result = run_backtest(development, config)
        except ValueError as error:
            raise InvalidRequest(str(error)) from error
        return self._save_report(result, persist)

    def run_walk_forward(
        self,
        config: WalkForwardConfig,
        *,
        interval: int = 60,
        limit: int = 720,
        persist: bool = True,
    ) -> BacktestRun:
        candles = self.candles(interval, limit)
        try:
            result = walk_forward(candles, config)
        except ValueError as error:
            raise InvalidRequest(str(error)) from error
        return self._save_report(result, persist)

    # --- orders -----------------------------------------------------------

    def get_order(self, order_id: str) -> OrderRecord:
        if not _ID.fullmatch(order_id):
            raise InvalidRequest("Invalid order identifier")
        try:
            spot = self.store.get_spot_order(order_id)
            fills = self.store.paper_fills(limit=100) if spot is None else []
        except _LOCAL_ERRORS:
            raise Unavailable("Order storage unavailable") from None
        if spot is not None:
            return OrderRecord("spot", spot_order_dict(spot))
        for fill in fills:
            if fill.order.idempotency_key == order_id:
                return OrderRecord(
                    "paper",
                    {
                        "idempotency_key": fill.order.idempotency_key,
                        "side": fill.order.side.value,
                        "quantity": str(fill.order.quantity),
                        "price": str(fill.price),
                        "fee": str(fill.fee),
                        "executed_at": fill.executed_at.isoformat(),
                    },
                )
        raise NotFound(f"Order '{order_id}' not found")

    def preview_order(
        self,
        side: PaperSide,
        quantity: Decimal,
        *,
        quote: Quote | None = None,
        idempotency_key: str | None = None,
    ) -> OrderPreview:
        """Evaluate a paper order against the same rules as submission.

        No ledger or order row is written.
        """
        try:
            order = PaperOrder(idempotency_key or "preview", side, quantity)
        except ValueError as error:
            raise InvalidRequest(str(error)) from error
        reasons: list[str] = []
        paused = self.execution_pause().paused
        if paused:
            reasons.append("Execution is paused")
        duplicate = False
        if idempotency_key is not None:
            try:
                duplicate = self.store.existing_paper_fill(order) is not None
            except PaperConflict as error:
                raise Rejected(str(error)) from error
            except _LOCAL_ERRORS:
                raise Unavailable("Paper ledger unavailable") from None
        quote = quote or self.latest_quote()
        now = datetime.now(UTC)
        price: Decimal | None = None
        fee: Decimal | None = None
        notional: Decimal | None = None
        cash_after: Decimal | None = None
        quantity_after: Decimal | None = None
        quality: DataQuality | None = None
        if quote is None:
            reasons.append("No BTC/USD quote is available")
        elif quote.market != BTC_USD:
            reasons.append("Paper execution supports BTC/USD only")
        else:
            quality = quote.quality_at(
                now, max_age=_MAX_QUOTE_AGE, max_clock_skew=_MAX_CLOCK_SKEW
            )
            if quality is not DataQuality.FRESH:
                reasons.append(f"Quote is {quality.value}; a fresh quote is required")
            else:
                try:
                    price, fee = simulated_terms(order, quote)
                    notional = price * quantity
                    after = book_fill(
                        self.paper_balance(), order, price, fee, now, quote
                    ).balance
                    cash_after, quantity_after = after.cash, after.quantity
                except PaperRejected as error:
                    reasons.append(str(error))
        return OrderPreview(
            would_fill=not reasons,
            reasons=reasons,
            side=side.value,
            quantity=quantity,
            price=price,
            fee=fee,
            notional=notional,
            cash_after=cash_after,
            quantity_after=quantity_after,
            quote_quality=quality,
            execution_paused=paused,
            duplicate_of_existing_fill=duplicate,
        )

    async def place_paper_order(self, order: PaperOrder) -> PaperFill:
        if self.paper is None:
            raise Forbidden("Paper execution is disabled")
        try:
            return await self.paper.submit(order)
        except (PaperRejected, PaperConflict) as error:
            raise Rejected(str(error)) from error
        except _LOCAL_ERRORS:
            raise Unavailable("Paper ledger unavailable") from None
