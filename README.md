# HCS Trading Intelligence

Made with ❤️ by HCS.media.

HCS Trading Intelligence is a local-first crypto research project. This development build includes a read-only dashboard, API and opt-in Kraken BTC/USD quotes. `PAPER` is the default policy mode. Paper orders, wallet access, MCP and live execution are planned.

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

Start with `uv run --locked hcs-trader serve --public-market-data` to opt into `DATA_ONLY` mode. Request `GET /api/v1/market/quotes` for the BTC/USD best bid and ask, timestamps and quality status. See the [data source notes](docs/DATA_SOURCES.md).

See [AGENTS.md](AGENTS.md) for the roadmap and release gate and [SECURITY.md](SECURITY.md) for vulnerability reporting.

Licensed under Apache-2.0. Made with love by timfromhcs and hcsmedia.
