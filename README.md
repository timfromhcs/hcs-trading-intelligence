# HCS Trading Intelligence

Made with ❤️ by HCS.media.

HCS Trading Intelligence is a local-first crypto research project. The dashboard and API expose archived market data, backtests, a simulated portfolio, wallet snapshots, spot order history, audit events and strategy candidates. The default `PAPER` mode starts with external network access disabled. Use `serve --paper-market-data` to enable simulated BTC/USD orders using Kraken quotes. Live order submission through the local service remains an open milestone.

![Dashboard in PAPER mode with a fresh local database](docs/dashboard.png)

## Developer setup

Use Python 3.12, uv 0.13.5, and Node.js 22.13 or later in the 22 series.

```sh
uv sync --locked
npm ci --prefix frontend
npm run build --prefix frontend
uv run --locked hcs-trader serve
```

Open the dashboard on the loopback address `http://127.0.0.1:8765`. The service exposes its schema at `/openapi.json` and provides health, mode and capability information under `/api/v1`. It stores startup audit events in SQLite in the operating system's application data directory.

Run `uv run --locked pytest` for the Python suite. Build the frontend before running `uv build` to package the dashboard with the wheel.

See the [signing architecture](docs/SIGNING.md) and [execution architecture](docs/EXECUTION.md) for key isolation, pre-trade risk policy, and fail-closed spot routing.

See the [desktop build guide](docs/RELEASE.md) for native bundle creation and verification.

Local MCP clients can read archived data using the [stdio server](docs/MCP.md).

`hcs-trader research-brief` returns an offline indicator summary with optional [local llama.cpp commentary](docs/LOCAL_RESEARCH.md).

Start with `uv run --locked hcs-trader serve --public-market-data` to opt into `DATA_ONLY` mode. The BTC/USD dashboard panel and `GET /api/v1/market/quotes` show the best bid and ask, timestamps and quote quality. See the [data source notes](docs/DATA_SOURCES.md).

`GET /api/v1/market/quotes/history?limit=100` reads previously stored quotes from SQLite and works with provider access disabled.

Run `uv run --locked hcs-trader import-candles --interval 60` to fetch and store closed Kraken BTC/USD hourly candles. `GET /api/v1/market/candles?interval=60` reads the stored candles offline in chronological order.

Download the same candles as CSV from `GET /api/v1/market/candles.csv?interval=60`. Both formats support the `limit` query parameter and include source and observation timestamps.

`GET /api/v1/market/indicators?interval=60` computes technical indicators offline from the latest contiguous segment of stored candles. [Indicator definitions](docs/INDICATORS.md)

`POST /api/v1/backtests?interval=60` runs the offline SMA research simulation. Use an empty JSON object for defaults. [Backtest assumptions](docs/BACKTEST.md)

Strategy Lab can generate and link backtest and walk-forward reports from archived hourly candles. [Workflow and screenshot](docs/BACKTEST.md#separately-held-evaluation-data)

Run `uv run --locked hcs-trader import-news` to import Federal Reserve monetary-policy release metadata. The dashboard and `GET /api/v1/news/events` read the stored timeline offline.

Run `uv run --locked hcs-trader import-macro` to import daily ECB USD-per-EUR reference rates. `GET /api/v1/macro/series` reads the locally observed values and revisions offline.

Run `uv run --locked hcs-trader import-fred --as-of YYYY-MM-DD` in a terminal for a US CPI snapshot. Enter your own FRED API key at the hidden prompt. Retrieve saved values with `GET /api/v1/macro/series?source=fred`.

See the [implementation plan](docs/ROADMAP.md) for remaining work, the [release assessment](docs/V1_RELEASE_REPORT.md) for verified evidence and [AGENTS.md](AGENTS.md) for the product requirements. See [SECURITY.md](SECURITY.md) for vulnerability reporting.

Licensed under Apache-2.0. Made with love by timfromhcs and hcsmedia.
