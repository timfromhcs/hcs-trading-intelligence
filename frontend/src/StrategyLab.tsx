import { useEffect, useState } from "react";

interface IndicatorsData {
  contiguous_candles: number;
  closed_at: string | null;
  rsi14: string | null;
  sma20: string | null;
  ema20: string | null;
  macd: string | null;
  macd_signal: string | null;
  macd_histogram: string | null;
  atr14: string | null;
  bollinger_upper: string | null;
  bollinger_lower: string | null;
  volatility20: string | null;
  volume_sma20: string | null;
}

interface SimulatedFill {
  side: "buy" | "sell";
  signal_at: string;
  filled_at: string;
  quantity: string;
  price: string;
  fee: string;
}

interface BacktestResult {
  strategy: string;
  dataset_sha256: string;
  candle_count: number;
  ending_cash: string;
  ending_quantity: string;
  ending_equity: string;
  net_return: string;
  max_drawdown: string;
  total_fees: string;
  fills: SimulatedFill[];
}

type IndicatorsState =
  | { state: "loading" }
  | { state: "unavailable" }
  | { state: "ready"; indicators: IndicatorsData };

const timeFormat = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

function formatDecimal(val: string | null, digits = 2): string {
  if (val === null) return "—";
  const num = Number(val);
  return Number.isFinite(num) ? num.toFixed(digits) : val;
}

export function StrategyLab() {
  const [indState, setIndState] = useState<IndicatorsState>({ state: "loading" });
  const [fastPeriod, setFastPeriod] = useState(10);
  const [slowPeriod, setSlowPeriod] = useState(30);
  const [backtestRunning, setBacktestRunning] = useState(false);
  const [backtestResult, setBacktestResult] = useState<BacktestResult | null>(null);
  const [backtestError, setBacktestError] = useState<string | null>(null);
  const [reportId, setReportId] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/v1/market/indicators?interval=60", {
      signal: controller.signal,
      cache: "no-store",
    })
      .then((res) => {
        if (!res.ok) throw new Error("Indicators unavailable");
        return res.json() as Promise<IndicatorsData>;
      })
      .then(
        (indicators) => setIndState({ state: "ready", indicators }),
        () => setIndState({ state: "unavailable" }),
      );
    return () => controller.abort();
  }, []);

  async function handleRunBacktest(e: React.FormEvent) {
    e.preventDefault();
    if (fastPeriod >= slowPeriod) {
      setBacktestError("Fast window must be strictly smaller than slow window");
      return;
    }
    setBacktestRunning(true);
    setBacktestError(null);
    setBacktestResult(null);
    setReportId(null);

    try {
      const response = await fetch("/api/v1/backtests?interval=60", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          fast_period: fastPeriod,
          slow_period: slowPeriod,
          starting_cash: "10000",
          allocation: "0.10",
        }),
      });

      if (!response.ok) {
        const err = await response.json().catch(() => ({ detail: "Simulation failed" }));
        throw new Error(err.detail || "Backtest execution failed");
      }

      const loc = response.headers.get("Location");
      if (loc) {
        const id = loc.split("/").pop();
        if (id) setReportId(id);
      }

      const result = (await response.json()) as BacktestResult;
      setBacktestResult(result);
    } catch (err: unknown) {
      setBacktestError(err instanceof Error ? err.message : "Backtest execution error");
    } finally {
      setBacktestRunning(false);
    }
  }

  return (
    <section className="panel" aria-labelledby="lab-heading">
      <div className="section-heading">
        <h2 id="lab-heading">Strategy Lab &amp; Quantitative Indicators</h2>
        <a
          href="/api/v1/market/candles.csv?interval=60"
          download="btc-usd-60m.csv"
          className="btn-link"
        >
          Export Candles CSV
        </a>
      </div>

      <div className="lab-subtitle muted">
        Deterministic indicator calculations and realistic fee-adjusted backtesting
        over contiguous closed hourly candles.
      </div>

      {indState.state === "loading" && <p role="status">Computing indicators…</p>}
      {indState.state === "unavailable" && (
        <p role="status">Technical indicators unavailable. Requires closed candle history.</p>
      )}

      {indState.state === "ready" && (
        <div className="status-grid indicators-grid">
          <div className="indicator-card">
            <dt>RSI (14)</dt>
            <dd>
              <span className="indicator-val">
                {formatDecimal(indState.indicators.rsi14, 1)}
              </span>
              {indState.indicators.rsi14 && Number(indState.indicators.rsi14) >= 70 && (
                <span className="badge-alert badge-sell">Overbought</span>
              )}
              {indState.indicators.rsi14 && Number(indState.indicators.rsi14) <= 30 && (
                <span className="badge-alert badge-buy">Oversold</span>
              )}
            </dd>
          </div>

          <div className="indicator-card">
            <dt>MACD (12, 26, 9)</dt>
            <dd>
              <span className="indicator-val">
                {formatDecimal(indState.indicators.macd, 2)}
              </span>
              <span className="indicator-sub">
                Signal: {formatDecimal(indState.indicators.macd_signal, 2)} · Hist:{" "}
                {formatDecimal(indState.indicators.macd_histogram, 2)}
              </span>
            </dd>
          </div>

          <div className="indicator-card">
            <dt>Moving Averages</dt>
            <dd>
              <span className="indicator-val">
                SMA20: {formatDecimal(indState.indicators.sma20, 1)}
              </span>
              <span className="indicator-sub">
                EMA20: {formatDecimal(indState.indicators.ema20, 1)}
              </span>
            </dd>
          </div>

          <div className="indicator-card">
            <dt>Bollinger Bands (20, 2σ)</dt>
            <dd>
              <span className="indicator-val">
                Upper: {formatDecimal(indState.indicators.bollinger_upper, 0)}
              </span>
              <span className="indicator-sub">
                Lower: {formatDecimal(indState.indicators.bollinger_lower, 0)}
              </span>
            </dd>
          </div>

          <div className="indicator-card">
            <dt>Volatility &amp; ATR (14)</dt>
            <dd>
              <span className="indicator-val">
                ATR: {formatDecimal(indState.indicators.atr14, 2)} USD
              </span>
              <span className="indicator-sub">
                Candles: {indState.indicators.contiguous_candles}
              </span>
            </dd>
          </div>
        </div>
      )}

      <div className="backtest-section">
        <h3>Trend Strategy Simulation</h3>
        <form onSubmit={handleRunBacktest} className="backtest-form">
          <div className="form-group">
            <label htmlFor="fast-window">Fast SMA Window</label>
            <input
              id="fast-window"
              type="number"
              min="2"
              max="199"
              value={fastPeriod}
              onChange={(e) => setFastPeriod(Number(e.target.value))}
            />
          </div>

          <div className="form-group">
            <label htmlFor="slow-window">Slow SMA Window</label>
            <input
              id="slow-window"
              type="number"
              min="3"
              max="200"
              value={slowPeriod}
              onChange={(e) => setSlowPeriod(Number(e.target.value))}
            />
          </div>

          <div className="form-group form-actions">
            <button type="submit" disabled={backtestRunning}>
              {backtestRunning ? "Simulating…" : "Run Backtest"}
            </button>
          </div>
        </form>

        {backtestError && (
          <div role="alert" className="alert-banner alert-danger">
            {backtestError}
          </div>
        )}

        {backtestResult && (
          <div className="backtest-results">
            <h4>Simulation Output ({backtestResult.strategy})</h4>
            <div className="status-grid result-grid">
              <div>
                <dt>Net Return</dt>
                <dd
                  className={
                    Number(backtestResult.net_return) >= 0 ? "available" : "quote-warning"
                  }
                >
                  {(Number(backtestResult.net_return) * 100).toFixed(2)}%
                </dd>
              </div>

              <div>
                <dt>Ending Equity</dt>
                <dd>{formatDecimal(backtestResult.ending_equity, 2)} USD</dd>
              </div>

              <div>
                <dt>Max Drawdown</dt>
                <dd className="quote-warning">
                  {(Number(backtestResult.max_drawdown) * 100).toFixed(2)}%
                </dd>
              </div>

              <div>
                <dt>Total Fees Paid</dt>
                <dd>{formatDecimal(backtestResult.total_fees, 2)} USD</dd>
              </div>

              <div>
                <dt>Simulated Fills</dt>
                <dd>{backtestResult.fills.length} trades</dd>
              </div>
            </div>

            {reportId && (
              <div className="report-meta muted">
                Report ID: <code>{reportId}</code> (Content-addressed SHA-256)
              </div>
            )}

            {backtestResult.fills.length > 0 && (
              <div className="backtest-fills">
                <h5>Recent Simulation Fills</h5>
                <ul className="paper-fills">
                  {backtestResult.fills.slice(-5).map((f, i) => (
                    <li key={`${f.filled_at}-${i}`}>
                      <strong>
                        {f.side.toUpperCase()} {formatDecimal(f.quantity, 4)} BTC
                      </strong>
                      <span>
                        @ {formatDecimal(f.price, 2)} USD · Fee: {formatDecimal(f.fee, 2)} USD
                      </span>
                      <time dateTime={f.filled_at}>
                        {timeFormat.format(new Date(f.filled_at))}
                      </time>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
