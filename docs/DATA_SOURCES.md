# Data sources

## Kraken Spot REST

- Source ID `kraken-spot`, operated by Kraken. The adapter requests top-of-book BTC/USD as JSON from the public Depth endpoint. [Endpoint documentation](https://docs.kraken.com/api-reference/market-data/get-order-book)
- Public access requires no API key. Kraken describes one request per second or less as within its public REST limit, with Depth limited by IP address. This application permits an on-demand refresh at most once every five seconds per running instance, applies bounded exponential backoff after failures and waits at least 60 seconds after a rate-limit response. [API limits](https://support.kraken.com/articles/206548367-what-are-the-api-rate-limits-)
- Quote quality uses the oldest side update and local UTC receipt. The last valid quote is marked degraded or stale during an outage, including after restart. Older updates cannot overwrite newer ones. Prices are returned as decimal strings.
- Distinct observed price levels and timestamps are persisted in local SQLite. The history endpoint reads up to 500 rows without network access and returns the most recently stored observations first. Repeated deliveries of the same quote retain the first local receipt.
- Disabled by default. Local usage is subject to the applicable Kraken terms. Public accessibility does not grant redistribution rights. [Terms by region](https://www.kraken.com/legal)
- The explicit `import-candles` command fetches up to 720 completed BTC/USD candles per call from Kraken OHLC. The interval is in minutes. The import keeps the first stored observation for each source, interval and opening time. The local candles API serves the latest requested rows in chronological order, with gaps preserved. [OHLC documentation](https://docs.kraken.com/api-reference/market-data/get-ohlc-data)

## Federal Reserve monetary-policy releases

- Source ID `federal-reserve-monetary-policy`, operated by the Board of Governors of the Federal Reserve System. The public RSS feed requires no API key. [Official RSS directory](https://www.federalreserve.gov/feeds/feeds.htm)
- `import-news` requests the feed once, without following redirects. Only monetary-policy release URLs on the official host are kept. The application stores plain title text, URL, publication time and first local receipt. Article bodies and feed descriptions are omitted.
- `GET /api/v1/news/events` reads locally stored source metadata in reverse publication order. Feed text is untrusted input.

## European Central Bank reference rates

- Source ID `ecb-sdmx`, series `EXR.D.USD.EUR.SP00.A`. Values are US dollars per euro. The public SDMX API requires no key. A single manual import fetches up to 250 daily observations. [ECB data API](https://data.ecb.europa.eu/help/api/data)
- Reference rates are economic observations. Trading venue quotes are supplied separately. The observation date describes the date of the reference rate, while `received_at` records when this installation first saw that value or revision. Initial downloads supply the currently published history. Historical publication times and complete historical vintages are outside this import's coverage. [ECB methodology](https://data.ecb.europa.eu/methodology/exchange-rates)
