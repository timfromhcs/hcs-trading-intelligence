# Changelog

## Unreleased

- Added canonical asset and market identities, exact monetary values, and explicit order and transaction transitions for later adapters.
- Added a loopback status API, responsive dashboard, and versioned SQLite startup audit history.
- Added opt-in Kraken BTC/USD quotes with bounded refresh, provider provenance and explicit data quality.
- Added deduplicated SQLite quote history and restart-safe fallback to the last stored quote.
- Added the opt-in BTC/USD dashboard panel with exact prices, source, timestamps and quote quality.
- Added explicit import of closed BTC/USD candles and offline candle history through the local API.
- Added offline CSV downloads for stored BTC/USD candles.
- Added an explicit monetary-policy release import and local, attributed news timeline.

## 0.1.0 — development foundation

- Added the installable CLI with default mode and capability reporting.
- Added locked development tooling and a three-platform verification workflow.
