# HCS Trading Intelligence

Made with ❤️ by HCS.media.

This repository contains the Phase 0 foundation for a local-first crypto research and spot-trading application. The CLI reports its capabilities. `PAPER` is the default policy mode. Market feeds, paper orders, wallet access, the API, MCP, and live execution are planned.

## Developer setup

Use Python 3.12, uv 0.13.5, and Node.js 22.

```sh
uv sync --locked
uv run --locked hcs-trader
uv run --locked pytest
npm ci --prefix frontend
```

See [AGENTS.md](AGENTS.md) for the roadmap and release gate and [SECURITY.md](SECURITY.md) for vulnerability reporting.

Licensed under Apache-2.0. Made with love by timfromhcs and hcsmedia.
