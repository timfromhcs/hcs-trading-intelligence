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

from hcs_trader.api import LOCAL_PORT, LOOPBACK_HOST, Status, create_app
from hcs_trader.data.ecb import MacroUnavailable, fetch_ecb_rates
from hcs_trader.data.fed_news import NewsUnavailable, fetch_fed_news
from hcs_trader.data.fred import fetch_fred_cpi
from hcs_trader.data.kraken import BTC_USD, CANDLE_INTERVALS, KrakenQuoteProvider
from hcs_trader.data.provider import QuoteUnavailable
from hcs_trader.data.quotes import QuoteService
from hcs_trader.storage import AuditStore


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


async def serve(market_data: bool) -> None:
    store = local_store()
    async with httpx2.AsyncClient(trust_env=False) as client:
        quotes = (
            QuoteService(
                KrakenQuoteProvider(client), BTC_USD, record_quote=store.record_quote
            )
            if market_data
            else None
        )
        app = create_app(
            store, Path(__file__).with_name("ui_assets"), quote_service=quotes
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
    serve_parser.add_argument(
        "--public-market-data",
        action="store_true",
        help="Enable public Kraken quote requests",
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
    arguments = parser.parse_args()
    if arguments.command == "serve":
        asyncio.run(serve(arguments.public_market_data))
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
    else:
        print(Status().model_dump_json())


if __name__ == "__main__":
    main()
