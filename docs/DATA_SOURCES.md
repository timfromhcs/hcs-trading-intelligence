# Data sources

## Kraken Spot REST

- Source ID `kraken-spot`, operated by Kraken. The adapter requests top-of-book BTC/USD as JSON from the public Depth endpoint. [Endpoint documentation](https://docs.kraken.com/api-reference/market-data/get-order-book)
- Public access requires no API key. Kraken describes one request per second or less as within its public REST limit, with Depth limited by IP address. This application permits an on-demand refresh at most once every five seconds per running instance, applies bounded exponential backoff after failures and waits at least 60 seconds after a rate-limit response. [API limits](https://support.kraken.com/articles/206548367-what-are-the-api-rate-limits-)
- Quote quality uses the oldest side update and local UTC receipt. The last valid quote is marked degraded or stale during an outage, including after restart. Older updates cannot overwrite newer ones. Prices are returned as decimal strings.
- Distinct observed price levels and timestamps are persisted in local SQLite. The history endpoint reads up to 500 rows without network access and returns the most recently stored observations first. Repeated deliveries of the same quote retain the first local receipt.
- Disabled by default. Local usage is subject to the applicable Kraken terms. Public accessibility does not grant redistribution rights. [Terms by region](https://www.kraken.com/legal)
