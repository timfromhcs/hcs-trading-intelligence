"""Expose the capabilities of the repository foundation."""

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
import warnings
import webbrowser
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import partial
from getpass import GetPassWarning, getpass
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from time import monotonic_ns
from uuid import uuid4

import httpx2
import uvicorn
from platformdirs import user_data_path
from pydantic import TypeAdapter

from hcs_trader.api import LOCAL_PORT, LOOPBACK_HOST, Status, create_app
from hcs_trader.data.ecb import MacroUnavailable, fetch_ecb_rates
from hcs_trader.data.fed_news import NewsUnavailable, fetch_fed_news
from hcs_trader.data.fred import fetch_fred_cpi
from hcs_trader.data.kraken import (
    BTC_USD,
    CANDLE_INTERVALS,
    SOURCE,
    KrakenQuoteProvider,
)
from hcs_trader.data.provider import QuoteUnavailable
from hcs_trader.data.quotes import QuoteService
from hcs_trader.domain.execution import SpotOrder, SpotOrderType, SpotSide
from hcs_trader.domain.lifecycle import OrderState
from hcs_trader.domain.paper import PaperRejected
from hcs_trader.domain.wallet import (
    SolanaBalance,
    TransactionObservation,
    WalletBalance,
    WalletSnapshot,
)
from hcs_trader.holdout import (
    CANDIDATE_JSON,
    MAX_HOLDOUT_BYTES,
    RESULT_JSON,
    freeze_candidate,
    score_holdout,
)
from hcs_trader.indicators import Indicators, calculate_indicators
from hcs_trader.kraken_spot import (
    KrakenExecutionError,
    KrakenOrderValidation,
    KrakenSpotAdapter,
)
from hcs_trader.local_research import (
    LlamaCppProvider,
    NoneModelProvider,
    ResearchHistory,
    ResearchReport,
    ResearchRun,
    research_report,
)
from hcs_trader.mcp_server import create_mcp_server
from hcs_trader.model_manifest import verify_model_manifest
from hcs_trader.promotion import (
    CandidateNotFound,
    PromotionRejected,
    PromotionStage,
    execute_shadow_run,
    list_shadow_runs_for_candidate,
    promote_strategy_candidate,
    watch_shadow,
)
from hcs_trader.risk import RiskEngine, RiskPolicy, RiskRejected
from hcs_trader.spot_execution import KrakenSpotExecution
from hcs_trader.storage import WALLET_SNAPSHOT, AuditStore
from hcs_trader.walk_forward import WalkForwardConfig
from hcs_trader.wallets import (
    ETHEREUM_NETWORKS,
    SOLANA_NETWORKS,
    UTXO_NETWORK_PROFILES,
    UTXO_NETWORKS,
    LocalWalletRpc,
    UtxoNetwork,
    WalletUnavailable,
)


def local_store() -> AuditStore:
    return AuditStore(
        user_data_path("HCS Trading Intelligence", "HCS.media") / "audit.sqlite"
    )


async def import_candles(interval: int) -> int:
    async with httpx2.AsyncClient(trust_env=False) as client:
        candles = await KrakenQuoteProvider(client).get_candles(BTC_USD, interval)
    store = local_store()
    store.initialize()
    return store.record_candles(candles)


async def import_news() -> int:
    async with httpx2.AsyncClient(trust_env=False) as client:
        items = await fetch_fed_news(client)
    store = local_store()
    store.initialize()
    return store.record_news(items)


async def import_macro() -> int:
    async with httpx2.AsyncClient(trust_env=False) as client:
        observations = await fetch_ecb_rates(client)
    store = local_store()
    store.initialize()
    return store.record_macro(observations)


async def import_fred(api_key: str, as_of: date | None) -> int:
    async with httpx2.AsyncClient(trust_env=False) as client:
        observations = await fetch_fred_cpi(client, api_key, as_of=as_of)
    store = local_store()
    store.initialize()
    return store.record_macro(observations)


def _read_fred_key() -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", GetPassWarning)
        try:
            return getpass("FRED API key: ")
        except (GetPassWarning, EOFError, KeyboardInterrupt):
            raise MacroUnavailable(
                "FRED import requires a private terminal prompt"
            ) from None


def freeze_research(
    commitment: str, interval: int, config_path: Path | None, limit: int = 1000
) -> None:
    if not 1 <= limit <= 1000:
        raise ValueError("Development candle limit must be between 1 and 1000")
    config = WalkForwardConfig()
    if config_path is not None:
        with config_path.open("rb") as handle:
            model = handle.read(16_385)
        if len(model) > 16_384:
            raise ValueError("Research configuration exceeds the supported size")
        config = TypeAdapter(WalkForwardConfig).validate_json(model)
    store = local_store()
    store.initialize()
    development = store.candle_history(BTC_USD, SOURCE, interval, limit=limit)
    candidate = freeze_candidate(development, config, commitment)
    payload = CANDIDATE_JSON.dump_json(candidate).decode("utf-8")
    store.freeze_holdout(commitment, payload)
    print(f"Frozen candidate for {commitment}")


def score_research(commitment: str, path: Path) -> None:
    store = local_store()
    store.initialize()
    payload = store.frozen_candidate(commitment)
    if payload is None:
        raise ValueError("No candidate is frozen for this holdout")
    candidate = CANDIDATE_JSON.validate_json(payload)
    if candidate.holdout_sha256 != commitment:
        raise ValueError("Stored candidate commitment is inconsistent")
    with path.open("rb") as handle:
        content = handle.read(MAX_HOLDOUT_BYTES + 1)
    outcome = score_holdout(candidate, content)
    result = RESULT_JSON.dump_json(outcome).decode("utf-8")
    print(store.finalize_holdout(commitment, result))


async def market_research(
    interval: int,
    port: int,
    alias: str | None,
    token_file: Path | None,
    manifest_path: Path | None,
    *,
    use_tools: bool = False,
    schedule: tuple[int, int] | None = None,
) -> ResearchReport:
    if use_tools and (manifest_path is None or token_file is None):
        raise ValueError("Research tools require a local model manifest and token file")
    started_at = datetime.now(UTC)
    started = monotonic_ns()
    store = local_store()
    store.initialize()
    evidence = calculate_indicators(
        store.candle_history(BTC_USD, SOURCE, interval, limit=720)
    )
    if alias is None and token_file is None and manifest_path is None:
        report = await research_report(evidence, NoneModelProvider())
    else:
        if token_file is None or manifest_path is None:
            raise ValueError(
                "A local model manifest and token file are required together"
            )
        report = await _configured_research(
            evidence, port, alias, token_file, manifest_path, use_tools=use_tools
        )
    next_schedule = None
    if schedule is not None:
        every_seconds, failures = schedule
        failures = min(failures + 1, 5) if report.status == "unavailable" else 0
        delay = min(every_seconds * 2**failures, 86400)
        next_schedule = (datetime.now(UTC) + timedelta(seconds=delay), failures)
    store.record_research_run(
        ResearchRun(
            run_id=uuid4().hex,
            started_at=started_at,
            duration_ms=(monotonic_ns() - started) // 1_000_000,
            interval_minutes=interval,
            status=report.status,
            model=report.model,
            model_sha256=report.verified_artifact.sha256
            if report.verified_artifact
            else None,
            prompt_version=report.prompt_version,
            evidence=report.evidence,
        ),
        schedule=next_schedule,
    )
    return report


async def watch_research(
    store: AuditStore, run: Callable[[int], Awaitable[ResearchReport]]
) -> None:
    store.initialize()
    with store.research_worker_lock():
        while True:
            next_run_at, failures = store.research_schedule()
            if next_run_at is not None:
                remaining = (next_run_at - datetime.now(UTC)).total_seconds()
                if remaining > 0:
                    await asyncio.sleep(min(remaining, 60))
                    continue
            report = await run(failures)
            print(
                TypeAdapter(ResearchReport).dump_json(report).decode("utf-8"),
                flush=True,
            )


async def _configured_research(
    evidence: Indicators,
    port: int,
    alias: str | None,
    token_file: Path,
    manifest_path: Path,
    *,
    use_tools: bool,
) -> ResearchReport:
    metadata = verify_model_manifest(manifest_path)
    if alias is not None and alias != metadata.alias:
        raise ValueError("Requested model alias does not match the manifest")
    with token_file.open(encoding="utf-8") as handle:
        token = handle.read(1025).strip()
    if not token or len(token) > 1024 or any(char.isspace() for char in token):
        raise ValueError("Local model token file must contain one token")
    async with httpx2.AsyncClient(trust_env=False) as client:
        report = await research_report(
            evidence,
            LlamaCppProvider(client, port, metadata.alias, token, use_tools=use_tools),
        )
    return replace(
        report,
        verified_artifact=metadata,
        prompt_version="market-research-tools-1"
        if use_tools
        else report.prompt_version,
    )


def read_kraken_credentials(credentials_file: Path) -> tuple[str, str]:
    with credentials_file.open("rb") as handle:
        content = handle.read(4097)
    if len(content) > 4096:
        raise ValueError("Credential file exceeds the supported size")
    try:
        credentials = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Expected a JSON credential object") from None
    if not isinstance(credentials, dict):
        raise ValueError("Expected a JSON credential object")
    key, secret = credentials.get("api_key"), credentials.get("api_secret")
    if (
        not isinstance(key, str)
        or not isinstance(secret, str)
        or not key
        or not secret
        or any(char.isspace() for char in key + secret)
    ):
        raise ValueError("Expected api_key and api_secret strings in credential file")
    return key, secret


async def validate_kraken_order(
    credentials_file: Path, side: SpotSide, quantity: Decimal, price: Decimal
) -> KrakenOrderValidation:
    if (
        not quantity.is_finite()
        or not price.is_finite()
        or not Decimal("0.00000001") <= quantity <= 100
        or not Decimal("0.01") <= price <= 10_000_000
        or quantity != round(quantity, 8)
        or price != round(price, 2)
        or not 10 <= quantity * price <= 1000
    ):
        raise ValueError(
            "Use a BTC/USD limit order of 10–1000 USD with exact precision"
        )
    key, secret = read_kraken_credentials(credentials_file)
    at = datetime.now(UTC)
    order = SpotOrder(
        client_order_id=uuid4().hex,
        order_id=uuid4().hex,
        venue="kraken",
        market="BTC/USD",
        side=side,
        order_type=SpotOrderType.LIMIT,
        quantity=quantity,
        limit_price=price,
        state=OrderState.PREVIEWED,
        risk_decision_id="validation-only",
        created_at=at,
        updated_at=at,
    )
    async with httpx2.AsyncClient(trust_env=False) as client:
        adapter = KrakenSpotAdapter(key, secret, client, validate_only=True)
        return await adapter.validate_spot_order(order, at)


async def ethereum_balance(
    address: str, port: int, network: str = "mainnet"
) -> WalletBalance:
    async with httpx2.AsyncClient(trust_env=False) as client:
        return await LocalWalletRpc(client, port).ethereum_balance(
            address, network=network
        )


async def solana_balance(
    address: str, port: int, network: str = "mainnet-beta"
) -> SolanaBalance:
    async with httpx2.AsyncClient(trust_env=False) as client:
        return await LocalWalletRpc(client, port).solana_balance(
            address, network=network
        )


async def transaction_status(
    chain: str, identifier: str, port: int | None
) -> TransactionObservation:
    async with httpx2.AsyncClient(trust_env=False) as client:
        if chain in {"eth", "eth-sepolia"}:
            network = "sepolia" if chain == "eth-sepolia" else "mainnet"
            return await LocalWalletRpc(
                client, 8545 if port is None else port
            ).ethereum_transaction(identifier, network=network)
        network = "devnet" if chain == "sol-devnet" else "mainnet-beta"
        return await LocalWalletRpc(
            client, 8899 if port is None else port
        ).solana_transaction(identifier, network=network)


async def utxo_balance(
    address: str, port: int, cookie_file: Path, network: UtxoNetwork
) -> WalletBalance:
    with cookie_file.open(encoding="utf-8") as handle:
        cookie = handle.read(1025).strip()
    username, separator, credential = cookie.partition(":")
    if (
        len(cookie) > 1024
        or username != "__cookie__"
        or not separator
        or not credential
        or any(char.isspace() for char in credential)
    ):
        raise ValueError(f"Expected a {network.name} Core authentication cookie")
    async with httpx2.AsyncClient(
        auth=httpx2.BasicAuth(username, credential), trust_env=False
    ) as client:
        return await LocalWalletRpc(client, port).utxo_balance(address, network)


async def serve(
    market_data: bool,
    paper_mode: bool = False,
    *,
    data_dir: Path | None = None,
    open_browser: bool = False,
    kraken_reconcile_credentials: Path | None = None,
) -> None:
    store = local_store() if data_dir is None else AuditStore(data_dir / "audit.sqlite")
    async with httpx2.AsyncClient(trust_env=False) as client:
        quotes = (
            QuoteService(
                KrakenQuoteProvider(client), BTC_USD, record_quote=store.record_quote
            )
            if market_data or paper_mode
            else None
        )
        recovery = None
        if kraken_reconcile_credentials is not None:
            key, secret = read_kraken_credentials(kraken_reconcile_credentials)
            execution = KrakenSpotExecution(
                store,
                KrakenSpotAdapter(key, secret, client, validate_only=True),
                RiskEngine(RiskPolicy()),
            )
            recovery = execution.reconcile_startup
        app = create_app(
            store,
            Path(__file__).with_name("ui_assets"),
            quote_service=quotes,
            paper_mode=paper_mode,
            kraken_reconciliation=recovery,
        )
        config = uvicorn.Config(
            app,
            host=LOOPBACK_HOST,
            port=LOCAL_PORT,
            proxy_headers=False,
            access_log=False,
        )
        server = uvicorn.Server(config)
        if open_browser:
            async with asyncio.TaskGroup() as group:
                run = group.create_task(server.serve())
                while not server.started and not run.done():
                    await asyncio.sleep(0.05)
                if server.started:
                    await asyncio.to_thread(
                        webbrowser.open, f"http://{LOOPBACK_HOST}:{LOCAL_PORT}/"
                    )
        else:
            await server.serve()


def main() -> None:
    parser = argparse.ArgumentParser(description="HCS Trading Intelligence")
    parser.add_argument("--version", action="version", version=version("hcs-trader"))
    commands = parser.add_subparsers(dest="command")
    serve_parser = commands.add_parser(
        "serve", help="Run the read-only local status service"
    )
    network_mode = serve_parser.add_mutually_exclusive_group()
    network_mode.add_argument(
        "--public-market-data",
        action="store_true",
        help="Enable public Kraken quote requests",
    )
    serve_parser.add_argument(
        "--data-dir", type=Path, help="Use a separate local data directory"
    )
    serve_parser.add_argument(
        "--open-browser", action="store_true", help="Open the dashboard"
    )
    serve_parser.add_argument(
        "--kraken-reconcile-credentials",
        type=Path,
        help="Opt in to read-only Kraken order recovery on startup",
    )
    network_mode.add_argument(
        "--paper-market-data",
        action="store_true",
        help="Enable paper BTC/USD orders using public Kraken quotes",
    )
    import_parser = commands.add_parser(
        "import-candles", help="Import closed Kraken BTC/USD candles into local history"
    )
    import_parser.add_argument(
        "--interval", type=int, choices=sorted(CANDLE_INTERVALS), default=60
    )
    commands.add_parser("import-news", help="Import official Federal Reserve releases")
    commands.add_parser(
        "import-macro", help="Import daily ECB USD-per-EUR reference rates"
    )
    fred_parser = commands.add_parser("import-fred", help="Import a FRED CPI snapshot")
    fred_parser.add_argument("--as-of", type=date.fromisoformat)
    freeze_parser = commands.add_parser(
        "freeze-research", help="Freeze a research candidate against a holdout hash"
    )
    freeze_parser.add_argument("commitment")
    freeze_parser.add_argument(
        "--interval", type=int, choices=sorted(CANDLE_INTERVALS), default=60
    )
    freeze_parser.add_argument("--config", type=Path)
    freeze_parser.add_argument("--limit", type=int, default=1000)
    score_parser = commands.add_parser(
        "score-holdout", help="Score a frozen candidate using the committed dataset"
    )
    score_parser.add_argument("commitment")
    score_parser.add_argument("file", type=Path)
    commands.add_parser("pause-execution", help="Persist the local execution pause")
    commands.add_parser("resume-execution", help="Clear the local execution pause")
    commands.add_parser("mcp", help="Run the read-only local MCP server on stdio")
    kraken_parser = commands.add_parser(
        "kraken-validate", help="Validate a BTC/USD limit order without submitting it"
    )
    kraken_parser.add_argument("--credentials-file", type=Path, required=True)
    kraken_parser.add_argument("--side", choices=list(SpotSide), required=True)
    kraken_parser.add_argument("--quantity", required=True)
    kraken_parser.add_argument("--limit-price", required=True)
    allow_canary = commands.add_parser(
        "spot-canary-allow",
        help="Locally authorize one Kraken BTC/USD canary candidate",
    )
    allow_canary.add_argument("candidate_id")
    allow_canary.add_argument("--credentials-file", type=Path, required=True)
    allow_canary.add_argument("--max-notional", required=True)
    commands.add_parser(
        "spot-canary-revoke", help="Revoke the local spot canary approval"
    )
    strategy_list = commands.add_parser(
        "strategy-list", help="List registered strategy candidates"
    )
    strategy_list.add_argument(
        "--stage",
        choices=[s.value for s in PromotionStage],
        help="Filter candidates by promotion stage",
    )
    strategy_list.add_argument(
        "--data-dir", type=Path, help="Use a separate local data directory"
    )
    strategy_show = commands.add_parser(
        "strategy-show", help="Show details of a registered strategy candidate"
    )
    strategy_show.add_argument("candidate_id", help="Candidate identifier")
    strategy_show.add_argument(
        "--data-dir", type=Path, help="Use a separate local data directory"
    )
    strategy_promote = commands.add_parser(
        "strategy-promote", help="Promote a strategy candidate to the next stage"
    )
    strategy_promote.add_argument("candidate_id", help="Candidate identifier")
    strategy_promote.add_argument(
        "--stage",
        choices=[s.value for s in PromotionStage],
        help="Target promotion stage (defaults to next stage in ladder)",
    )
    strategy_promote.add_argument(
        "--data-dir", type=Path, help="Use a separate local data directory"
    )
    strategy_shadow = commands.add_parser(
        "strategy-shadow-run",
        help="Execute and persist a paper shadow replay for a candidate",
    )
    strategy_shadow.add_argument("candidate_id", help="Candidate identifier")
    strategy_shadow.add_argument(
        "--data-dir", type=Path, help="Use a separate local data directory"
    )
    strategy_shadow_history = commands.add_parser(
        "strategy-shadow-history",
        help="Read shadow replay history and metrics for a candidate",
    )
    strategy_shadow_history.add_argument("candidate_id", help="Candidate identifier")
    strategy_shadow_history.add_argument(
        "--limit", type=int, default=25, help="Max runs to return"
    )
    strategy_shadow_history.add_argument(
        "--data-dir", type=Path, help="Use a separate local data directory"
    )
    strategy_shadow_watch = commands.add_parser(
        "strategy-shadow-watch",
        help="Foreground scheduled observer for forward shadow execution",
    )
    strategy_shadow_watch.add_argument(
        "--candidate-id", help="Optional candidate identifier filter"
    )
    strategy_shadow_watch.add_argument(
        "--every-seconds",
        type=int,
        default=3600,
        help="Observation interval in seconds",
    )
    strategy_shadow_watch.add_argument(
        "--data-dir", type=Path, help="Use a separate local data directory"
    )
    research_parser = argparse.ArgumentParser(add_help=False)
    research_parser.add_argument(
        "--interval", type=int, choices=sorted(CANDLE_INTERVALS), default=60
    )
    research_parser.add_argument("--llama-port", type=int, default=9931)
    research_parser.add_argument("--model-alias")
    research_parser.add_argument("--model-manifest", type=Path)
    research_parser.add_argument("--token-file", type=Path)
    research_parser.add_argument(
        "--research-tools",
        action="store_true",
        help="Enable one archived indicator tool call",
    )
    commands.add_parser(
        "research-brief", parents=[research_parser], help="Run one local research brief"
    )
    watch_parser = commands.add_parser(
        "research-watch",
        parents=[research_parser],
        help="Run local research on a schedule",
    )
    watch_parser.add_argument("--every-seconds", type=int, default=3600)
    history_parser = commands.add_parser(
        "research-history", help="Read local research run history and statistics"
    )
    history_parser.add_argument("--limit", type=int, default=25)
    wallet_parser = commands.add_parser(
        "eth-balance", help="Read ETH balance from a local node"
    )
    wallet_parser.add_argument("address")
    wallet_parser.add_argument("--port", type=int, default=8545)
    wallet_parser.add_argument(
        "--network", choices=ETHEREUM_NETWORKS, default="mainnet"
    )
    wallet_parser.add_argument(
        "--save", action="store_true", help="Save the observed balance locally"
    )
    sol_parser = commands.add_parser(
        "sol-balance", help="Read SOL balance from a local node"
    )
    sol_parser.add_argument("address")
    sol_parser.add_argument("--port", type=int, default=8899)
    sol_parser.add_argument(
        "--network", choices=SOLANA_NETWORKS, default="mainnet-beta"
    )
    sol_parser.add_argument(
        "--save", action="store_true", help="Save the observed balance locally"
    )
    for command, network in UTXO_NETWORKS.items():
        node_parser = commands.add_parser(
            command, help=f"Scan {network.name} UTXOs at a local node"
        )
        node_parser.add_argument("address")
        node_parser.add_argument("--port", type=int)
        node_parser.add_argument(
            "--network", choices=UTXO_NETWORK_PROFILES[command], default="mainnet"
        )
        node_parser.add_argument("--cookie-file", type=Path, required=True)
        node_parser.add_argument(
            "--save", action="store_true", help="Save the observed balance locally"
        )
    tx_parser = commands.add_parser(
        "tx-status", help="Read a transaction status from a local node"
    )
    tx_parser.add_argument("chain", choices=["eth", "eth-sepolia", "sol", "sol-devnet"])
    tx_parser.add_argument("identifier")
    tx_parser.add_argument("--port", type=int)
    arguments = parser.parse_args()
    if arguments.command == "serve":
        try:
            asyncio.run(
                serve(
                    arguments.public_market_data,
                    arguments.paper_market_data,
                    data_dir=arguments.data_dir,
                    open_browser=arguments.open_browser,
                    kraken_reconcile_credentials=arguments.kraken_reconcile_credentials,
                )
            )
        except (OSError, ValueError) as error:
            parser.exit(1, f"{error}\n")
    elif arguments.command == "import-candles":
        try:
            count = asyncio.run(import_candles(arguments.interval))
        except QuoteUnavailable as error:
            parser.exit(1, f"{error}\n")
        print(f"Imported {count} new BTC/USD candles.")
    elif arguments.command == "import-news":
        try:
            count = asyncio.run(import_news())
        except NewsUnavailable as error:
            parser.exit(1, f"{error}\n")
        print(f"Imported {count} new Federal Reserve releases.")
    elif arguments.command == "import-macro":
        try:
            count = asyncio.run(import_macro())
        except MacroUnavailable as error:
            parser.exit(1, f"{error}\n")
        print(f"Imported {count} new or revised ECB observations.")
    elif arguments.command == "import-fred":
        # This CLI process makes one credential-bearing request. Suppress URL traces.
        logging.getLogger("httpx2").setLevel(logging.WARNING)
        logging.getLogger("httpcore2").setLevel(logging.WARNING)
        try:
            count = asyncio.run(import_fred(_read_fred_key(), arguments.as_of))
        except MacroUnavailable as error:
            parser.exit(1, f"{error}\n")
        print(f"Imported {count} new or revised FRED observations.")
    elif arguments.command in {"freeze-research", "score-holdout"}:
        try:
            if arguments.command == "freeze-research":
                freeze_research(
                    arguments.commitment,
                    arguments.interval,
                    arguments.config,
                    arguments.limit,
                )
            else:
                score_research(arguments.commitment, arguments.file)
        except (OSError, ValueError) as error:
            parser.exit(1, f"{error}\n")
    elif arguments.command in {"pause-execution", "resume-execution"}:
        paused = arguments.command == "pause-execution"
        store = local_store()
        store.initialize()
        store.set_execution_pause(paused)
        print("Execution paused." if paused else "Execution pause cleared.")
    elif arguments.command == "mcp":
        create_mcp_server(local_store().path).run(transport="stdio")
    elif arguments.command in {"spot-canary-allow", "spot-canary-revoke"}:
        store = local_store()
        try:
            store.initialize()
            if arguments.command == "spot-canary-revoke":
                store.revoke_spot_canary()
                print("Spot canary authorization cleared.")
                return
            if not sys.stdin.isatty():
                raise ValueError(
                    "Canary authorization requires a local interactive terminal"
                )
            cap = Decimal(arguments.max_notional)
            if not cap.is_finite() or not 10 <= cap <= 1000 or cap != round(cap, 2):
                raise ValueError(
                    "Use a USD cap of 10–1000 with at most two decimal places"
                )
            candidate = store.get_strategy_candidate(arguments.candidate_id)
            if candidate is None or candidate.stage not in {
                PromotionStage.PAPER_APPROVED,
                PromotionStage.LIVE_CANARY,
            }:
                raise ValueError("A paper-approved strategy candidate is required")
            key, _ = read_kraken_credentials(arguments.credentials_file)
            identity = sha256(key.encode("utf-8")).hexdigest()
            print(
                "Kraken BTC/USD spot limit orders · "
                f"candidate {candidate.candidate_id} "
                f"({candidate.version}) · API key fingerprint {identity[:12]} · "
                f"USD {cap:.2f} per order · 24 hours. Orders can lose money.",
                file=sys.stderr,
            )
            if (
                input("Type AUTHORIZE to replace the active canary authorization: ")
                != "AUTHORIZE"
            ):
                raise ValueError("Canary authorization was not confirmed")
            at = datetime.now(UTC)
            store.authorize_spot_canary(
                candidate, identity, cap, at, at + timedelta(hours=24)
            )
            print("Spot canary authorization saved. Server execution is unchanged.")
        except InvalidOperation:
            parser.exit(1, "Notional cap must be a decimal\n")
        except (EOFError, KeyboardInterrupt):
            parser.exit(1, "Canary authorization was not confirmed\n")
        except (OSError, sqlite3.Error, RiskRejected, ValueError) as error:
            parser.exit(1, f"{error}\n")
    elif arguments.command == "strategy-list":
        store = (
            local_store()
            if arguments.data_dir is None
            else AuditStore(arguments.data_dir / "audit.sqlite")
        )
        try:
            store.initialize()
            stage = PromotionStage(arguments.stage) if arguments.stage else None
            candidates = store.list_strategy_candidates(stage=stage)
        except (sqlite3.Error, ValueError) as error:
            parser.exit(1, f"{error}\n")
        print(json.dumps([c.model_dump(mode="json") for c in candidates], indent=2))
    elif arguments.command == "strategy-show":
        store = (
            local_store()
            if arguments.data_dir is None
            else AuditStore(arguments.data_dir / "audit.sqlite")
        )
        try:
            store.initialize()
            candidate = store.get_strategy_candidate(arguments.candidate_id)
        except (sqlite3.Error, ValueError) as error:
            parser.exit(1, f"{error}\n")
        if candidate is None:
            parser.exit(1, f"Strategy candidate '{arguments.candidate_id}' not found\n")
        data = candidate.model_dump(mode="json")
        shadow = store.latest_shadow_run(candidate.candidate_id)
        if shadow is not None:
            data["latest_shadow_run_id"] = shadow[0]
        data["has_canary_approval"] = store.has_spot_canary_approval(
            candidate.candidate_id, datetime.now(UTC)
        )
        print(json.dumps(data, indent=2))
    elif arguments.command == "strategy-promote":
        store = (
            local_store()
            if arguments.data_dir is None
            else AuditStore(arguments.data_dir / "audit.sqlite")
        )
        try:
            store.initialize()
            stage = PromotionStage(arguments.stage) if arguments.stage else None
            updated, verdict = promote_strategy_candidate(
                store, arguments.candidate_id, stage
            )
        except CandidateNotFound as error:
            parser.exit(1, f"{error}\n")
        except PromotionRejected as error:
            parser.exit(1, f"{error}\n")
        except (sqlite3.Error, ValueError) as error:
            parser.exit(1, f"{error}\n")
        print(
            f"Successfully promoted candidate '{updated.candidate_id}' "
            f"to {updated.stage.value}."
        )
        print(
            json.dumps(
                {
                    "candidate": updated.model_dump(mode="json"),
                    "gate_result": {
                        "from_stage": verdict.from_stage.value,
                        "to_stage": verdict.to_stage.value,
                        "passed": verdict.passed,
                        "reasons": verdict.reasons,
                        "evaluated_at": verdict.evaluated_at.isoformat(),
                    },
                },
                indent=2,
            )
        )
    elif arguments.command == "strategy-shadow-run":
        store = (
            local_store()
            if arguments.data_dir is None
            else AuditStore(arguments.data_dir / "audit.sqlite")
        )
        try:
            store.initialize()
            run_id, shadow_result = execute_shadow_run(store, arguments.candidate_id)
        except CandidateNotFound as error:
            parser.exit(1, f"{error}\n")
        except (PaperRejected, PromotionRejected) as error:
            parser.exit(1, f"{error}\n")
        except (sqlite3.Error, ValueError) as error:
            parser.exit(1, f"{error}\n")
        print(
            json.dumps(
                {
                    "candidate_id": arguments.candidate_id,
                    "run_id": run_id,
                    "dataset_closed_at": shadow_result.dataset_closed_at.isoformat(),
                    "net_return": str(shadow_result.net_return),
                    "max_drawdown": str(shadow_result.max_drawdown),
                    "fills_count": len(shadow_result.fills),
                },
                indent=2,
            )
        )
    elif arguments.command == "strategy-shadow-history":
        store = (
            local_store()
            if arguments.data_dir is None
            else AuditStore(arguments.data_dir / "audit.sqlite")
        )
        try:
            store.initialize()
            runs = list_shadow_runs_for_candidate(
                store, arguments.candidate_id, limit=arguments.limit
            )
        except (sqlite3.Error, ValueError) as error:
            parser.exit(1, f"{error}\n")
        print(
            json.dumps(
                [
                    {
                        "run_id": run_id,
                        "evaluated_until": evaluated_until.isoformat(),
                        "net_return": str(report.net_return),
                        "max_drawdown": str(report.max_drawdown),
                        "fills_count": len(report.fills),
                        "dataset_closed_at": report.dataset_closed_at.isoformat(),
                    }
                    for run_id, evaluated_until, report in runs
                ],
                indent=2,
            )
        )
    elif arguments.command == "strategy-shadow-watch":
        store = (
            local_store()
            if arguments.data_dir is None
            else AuditStore(arguments.data_dir / "audit.sqlite")
        )
        if not 60 <= arguments.every_seconds <= 86400:
            parser.exit(
                1, "Observation interval must be between 60 and 86400 seconds\n"
            )
        try:
            asyncio.run(
                watch_shadow(
                    store,
                    arguments.candidate_id,
                    every_seconds=arguments.every_seconds,
                )
            )
        except KeyboardInterrupt:
            return
        except (sqlite3.Error, ValueError) as error:
            parser.exit(1, f"{error}\n")
    elif arguments.command == "kraken-validate":
        try:
            validation = asyncio.run(
                validate_kraken_order(
                    arguments.credentials_file,
                    SpotSide(arguments.side),
                    Decimal(arguments.quantity),
                    Decimal(arguments.limit_price),
                )
            )
        except InvalidOperation:
            parser.exit(1, "Quantity and limit price must be decimals\n")
        except KrakenExecutionError:
            parser.exit(1, "Kraken did not confirm validation-only\n")
        except (OSError, ValueError) as error:
            parser.exit(1, f"{error}\n")
        print(TypeAdapter(KrakenOrderValidation).dump_json(validation).decode("utf-8"))
    elif arguments.command in {"research-brief", "research-watch"}:
        run = partial(
            market_research,
            arguments.interval,
            arguments.llama_port,
            arguments.model_alias,
            arguments.token_file,
            arguments.model_manifest,
            use_tools=arguments.research_tools,
        )
        try:
            if arguments.command == "research-watch":
                if not 60 <= arguments.every_seconds <= 86400:
                    raise ValueError(
                        "Research period must be between 60 and 86400 seconds"
                    )
                asyncio.run(
                    watch_research(
                        local_store(),
                        lambda failures: run(
                            schedule=(arguments.every_seconds, failures)
                        ),
                    )
                )
            else:
                store = local_store()
                store.initialize()
                with store.research_worker_lock():
                    report = asyncio.run(run())
                print(TypeAdapter(ResearchReport).dump_json(report).decode("utf-8"))
        except KeyboardInterrupt:
            return
        except (OSError, sqlite3.Error, ValueError) as error:
            parser.exit(1, f"{error}\n")
    elif arguments.command == "research-history":
        store = local_store()
        try:
            store.initialize()
            history = store.research_history(arguments.limit)
        except (sqlite3.Error, ValueError) as error:
            parser.exit(1, f"{error}\n")
        print(TypeAdapter(ResearchHistory).dump_json(history).decode("utf-8"))
    elif arguments.command in {"eth-balance", "sol-balance", *UTXO_NETWORKS}:
        try:
            snapshot: WalletSnapshot
            if arguments.command == "eth-balance":
                snapshot = asyncio.run(
                    ethereum_balance(
                        arguments.address, arguments.port, arguments.network
                    )
                )
            elif arguments.command == "sol-balance":
                snapshot = asyncio.run(
                    solana_balance(arguments.address, arguments.port, arguments.network)
                )
            else:
                network = UTXO_NETWORK_PROFILES[arguments.command][arguments.network]
                snapshot = asyncio.run(
                    utxo_balance(
                        arguments.address,
                        network.port if arguments.port is None else arguments.port,
                        arguments.cookie_file,
                        network,
                    )
                )
            if arguments.save:
                store = local_store()
                if arguments.network in {"sepolia", "devnet", "regtest", "signet"}:
                    profile = arguments.network
                    if arguments.command in {"ltc-balance", "dgb-balance"}:
                        profile = f"{network.name.lower()}-{profile}"
                    store = AuditStore(store.path.with_name(f"wallet-{profile}.sqlite"))
                store.initialize()
                store.save_wallet_snapshot(snapshot)
        except sqlite3.Error:
            parser.exit(1, "Wallet snapshot storage unavailable\n")
        except (OSError, WalletUnavailable, ValueError) as error:
            parser.exit(1, f"{error}\n")
        print(WALLET_SNAPSHOT.dump_json(snapshot).decode("utf-8"))
    elif arguments.command == "tx-status":
        try:
            observation = asyncio.run(
                transaction_status(
                    arguments.chain, arguments.identifier, arguments.port
                )
            )
        except (WalletUnavailable, ValueError) as error:
            parser.exit(1, f"{error}\n")
        print(
            TypeAdapter(TransactionObservation).dump_json(observation).decode("utf-8")
        )
    else:
        print(Status().model_dump_json())


if __name__ == "__main__":
    main()
