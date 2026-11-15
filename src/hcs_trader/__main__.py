"""Expose the capabilities of the repository foundation."""

import argparse
import asyncio
import logging
import warnings
from datetime import date
from getpass import GetPassWarning, getpass
from importlib.metadata import version
from pathlib import Path

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
from hcs_trader.holdout import (
    CANDIDATE_JSON,
    MAX_HOLDOUT_BYTES,
    RESULT_JSON,
    freeze_candidate,
    score_holdout,
)
from hcs_trader.indicators import calculate_indicators
from hcs_trader.local_research import (
    LlamaCppProvider,
    NoneModelProvider,
    ResearchReport,
    research_report,
)
from hcs_trader.mcp_server import create_mcp_server
from hcs_trader.storage import AuditStore
from hcs_trader.walk_forward import WalkForwardConfig


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


def freeze_research(commitment: str, interval: int, config_path: Path | None) -> None:
    config = WalkForwardConfig()
    if config_path is not None:
        with config_path.open("rb") as handle:
            model = handle.read(16_385)
        if len(model) > 16_384:
            raise ValueError("Research configuration exceeds the supported size")
        config = TypeAdapter(WalkForwardConfig).validate_json(model)
    store = local_store()
    store.initialize()
    development = store.candle_history(BTC_USD, SOURCE, interval, limit=1000)
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
    interval: int, port: int, alias: str | None, token_file: Path | None
) -> ResearchReport:
    store = local_store()
    store.initialize()
    evidence = calculate_indicators(
        store.candle_history(BTC_USD, SOURCE, interval, limit=720)
    )
    if alias is None and token_file is None:
        return await research_report(evidence, NoneModelProvider())
    if alias is None or token_file is None:
        raise ValueError("A local model alias and token file are required together")
    with token_file.open(encoding="utf-8") as handle:
        token = handle.read(1025).strip()
    if not token or len(token) > 1024 or any(char.isspace() for char in token):
        raise ValueError("Local model token file must contain one token")
    async with httpx2.AsyncClient(trust_env=False) as client:
        return await research_report(
            evidence, LlamaCppProvider(client, port, alias, token)
        )


async def serve(market_data: bool, paper_mode: bool = False) -> None:
    store = local_store()
    async with httpx2.AsyncClient(trust_env=False) as client:
        quotes = (
            QuoteService(
                KrakenQuoteProvider(client), BTC_USD, record_quote=store.record_quote
            )
            if market_data or paper_mode
            else None
        )
        app = create_app(
            store,
            Path(__file__).with_name("ui_assets"),
            quote_service=quotes,
            paper_mode=paper_mode,
        )
        config = uvicorn.Config(
            app,
            host=LOOPBACK_HOST,
            port=LOCAL_PORT,
            proxy_headers=False,
            access_log=False,
        )
        await uvicorn.Server(config).serve()


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
    score_parser = commands.add_parser(
        "score-holdout", help="Score a frozen candidate using the committed dataset"
    )
    score_parser.add_argument("commitment")
    score_parser.add_argument("file", type=Path)
    commands.add_parser("pause-execution", help="Persist the local execution pause")
    commands.add_parser("resume-execution", help="Clear the local execution pause")
    commands.add_parser("mcp", help="Run the read-only local MCP server on stdio")
    research_parser = commands.add_parser(
        "research-brief", help="Read an offline research brief"
    )
    research_parser.add_argument(
        "--interval", type=int, choices=sorted(CANDLE_INTERVALS), default=60
    )
    research_parser.add_argument("--llama-port", type=int, default=9931)
    research_parser.add_argument("--model-alias")
    research_parser.add_argument("--token-file", type=Path)
    arguments = parser.parse_args()
    if arguments.command == "serve":
        asyncio.run(serve(arguments.public_market_data, arguments.paper_market_data))
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
                    arguments.commitment, arguments.interval, arguments.config
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
    elif arguments.command == "research-brief":
        try:
            report = asyncio.run(
                market_research(
                    arguments.interval,
                    arguments.llama_port,
                    arguments.model_alias,
                    arguments.token_file,
                )
            )
        except (OSError, ValueError) as error:
            parser.exit(1, f"{error}\n")
        print(TypeAdapter(ResearchReport).dump_json(report).decode("utf-8"))
    else:
        print(Status().model_dump_json())


if __name__ == "__main__":
    main()
