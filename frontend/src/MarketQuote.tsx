import { useEffect, useState } from "react";

type Quality = "FRESH" | "DEGRADED" | "STALE" | "MISSING" | "INVALID";

interface Quote {
  quality: Quality;
  provider_issue: "rate_limited" | "unavailable" | "invalid" | "storage_unavailable" | null;
  bid: string | null;
  ask: string | null;
  bid_updated_at: string | null;
  ask_updated_at: string | null;
  received_at: string | null;
}

const qualityLabels: Record<Quality, string> = {
  FRESH: "Fresh",
  DEGRADED: "Degraded",
  STALE: "Stale",
  MISSING: "No quote yet",
  INVALID: "Invalid provider data",
};

const issues: Record<NonNullable<Quote["provider_issue"]>, string> = {
  rate_limited: "Kraken has limited requests.",
  unavailable: "Kraken is temporarily unavailable.",
  invalid: "The latest provider response was invalid.",
  storage_unavailable: "Local quote storage is unavailable.",
};

function Timestamp({ value }: { value: string | null }) {
  if (value === null) return null;
  return <time dateTime={value}>{new Date(value).toLocaleString()}</time>;
}

export function MarketQuote() {
  const [quote, setQuote] = useState<Quote | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    let retry: ReturnType<typeof setTimeout> | undefined;

    async function refresh() {
      try {
        const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]);
        const response = await fetch("/api/v1/market/quotes", { signal, cache: "no-store" });
        if (!response.ok) throw new Error("Quote API unavailable");
        const next = (await response.json()) as Quote;
        if (!controller.signal.aborted) {
          setQuote(next);
          setError(false);
        }
      } catch {
        if (!controller.signal.aborted) setError(true);
      } finally {
        if (!controller.signal.aborted) retry = setTimeout(refresh, 10_000);
      }
    }

    void refresh();
    return () => {
      controller.abort();
      clearTimeout(retry);
    };
  }, []);

  return (
    <section className="panel" aria-labelledby="market-heading">
      <div className="section-heading">
        <h2 id="market-heading">BTC/USD</h2>
        <a href="https://docs.kraken.com/api-reference/market-data/get-order-book">Kraken Spot</a>
      </div>
      <div role="status" className="quote-status">
        {error && <p className="quote-warning">Quote status unavailable.</p>}
        {!error && quote === null && <p>Loading quote…</p>}
        {!error && quote !== null && (
          <p>
            <strong className={quote.quality === "FRESH" ? "available" : "quote-warning"}>
              {qualityLabels[quote.quality]}
            </strong>
            {quote.provider_issue && <> · {issues[quote.provider_issue]}</>}
          </p>
        )}
      </div>
      {!error && quote !== null && quote.bid !== null && quote.ask !== null && (
        <>
          <dl className="status-grid quote-prices">
            <div>
              <dt>Best bid</dt>
              <dd>{quote.bid} USD</dd>
              <dd className="quote-time">Updated <Timestamp value={quote.bid_updated_at} /></dd>
            </div>
            <div>
              <dt>Best ask</dt>
              <dd>{quote.ask} USD</dd>
              <dd className="quote-time">Updated <Timestamp value={quote.ask_updated_at} /></dd>
            </div>
          </dl>
          <p className="quote-time">Received locally <Timestamp value={quote.received_at} /></p>
        </>
      )}
    </section>
  );
}
