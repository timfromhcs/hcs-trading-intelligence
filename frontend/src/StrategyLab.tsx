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

export interface StrategyCandidate {
  candidate_id: string;
  name: string;
  version: string;
  stage: string;
  parameters: Record<string, unknown>;
  dataset_hash: string;
  created_at: string;
  updated_at: string;
}

const PROMOTION_LADDER = [
  "RESEARCH",
  "UNIT_TESTED",
  "BACKTESTED",
  "WALK_FORWARD_PASSED",
  "PAPER_SHADOW",
  "PAPER_APPROVED",
  "LIVE_CANARY",
  "LIVE_BOUNDED",
] as const;

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

function getNextStage(current: string): string | null {
  const idx = PROMOTION_LADDER.indexOf(current as (typeof PROMOTION_LADDER)[number]);
  if (idx >= 0 && idx + 1 < PROMOTION_LADDER.length) {
    return PROMOTION_LADDER[idx + 1];
  }
  return null;
}

export function StrategyLab() {
  const [indState, setIndState] = useState<IndicatorsState>({ state: "loading" });
  const [fastPeriod, setFastPeriod] = useState(10);
  const [slowPeriod, setSlowPeriod] = useState(30);
  const [backtestRunning, setBacktestRunning] = useState(false);
  const [backtestResult, setBacktestResult] = useState<BacktestResult | null>(null);
  const [backtestError, setBacktestError] = useState<string | null>(null);
  const [reportId, setReportId] = useState<string | null>(null);

  const [candidates, setCandidates] = useState<StrategyCandidate[]>([]);
  const [loadingCandidates, setLoadingCandidates] = useState(false);
  const [candidateActionMessage, setCandidateActionMessage] = useState<string | null>(null);
  const [candidateActionError, setCandidateActionError] = useState<string | null>(null);
  const [registering, setRegistering] = useState(false);
  const [promotingId, setPromotingId] = useState<string | null>(null);

  async function loadCandidates() {
    setLoadingCandidates(true);
    try {
      const res = await fetch("/api/v1/strategies/candidates", { cache: "no-store" });
      if (res.ok) {
        const data = (await res.json()) as StrategyCandidate[];
        setCandidates(data);
      }
    } catch {
      // ignore
    } finally {
      setLoadingCandidates(false);
    }
  }

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
    loadCandidates();
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

  async function handleRegisterCandidate() {
    if (!backtestResult) return;
    setRegistering(true);
    setCandidateActionError(null);
    setCandidateActionMessage(null);
    try {
      const res = await fetch("/api/v1/strategies/candidates", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: `SMA Crossover (${fastPeriod}/${slowPeriod})`,
          version: "1.0.0",
          parameters: { fast: fastPeriod, slow: slowPeriod },
          dataset_hash: backtestResult.dataset_sha256,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Registration failed" }));
        throw new Error(err.detail || "Failed to register candidate");
      }
      setCandidateActionMessage("Candidate successfully registered in RESEARCH stage!");
      await loadCandidates();
    } catch (err: unknown) {
      setCandidateActionError(err instanceof Error ? err.message : "Error registering candidate");
    } finally {
      setRegistering(false);
    }
  }

  async function handlePromoteCandidate(candidate: StrategyCandidate) {
    const next = getNextStage(candidate.stage);
    if (!next) return;
    setPromotingId(candidate.candidate_id);
    setCandidateActionError(null);
    setCandidateActionMessage(null);
    try {
      const res = await fetch(`/api/v1/strategies/candidates/${candidate.candidate_id}/promote`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target_stage: next,
          shadow_fills: 5,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Promotion failed" }));
        throw new Error(err.detail || `Promotion gate rejected transition to ${next}`);
      }
      setCandidateActionMessage(`Successfully promoted candidate to ${next}!`);
      await loadCandidates();
    } catch (err: unknown) {
      setCandidateActionError(err instanceof Error ? err.message : "Error promoting candidate");
    } finally {
      setPromotingId(null);
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
          <div className="quote-warning" role="alert">
            {backtestError}
          </div>
        )}

        {backtestResult && (
          <div className="backtest-results">
            <h4>Backtest Performance ({backtestResult.strategy})</h4>
            <div className="status-grid result-grid">
              <div>
                <dt>Net Return</dt>
                <dd
                  className={
                    Number(backtestResult.net_return) >= 0
                      ? "positive-val"
                      : "negative-val"
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

            <div className="candidate-register-bar">
              <button
                type="button"
                className="btn-register"
                onClick={handleRegisterCandidate}
                disabled={registering}
              >
                {registering ? "Registering…" : "Register Strategy as Promotion Candidate"}
              </button>
            </div>

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

      <div className="candidates-section">
        <div className="section-heading">
          <h3>Strategy Promotion Ladder &amp; Candidates</h3>
          <button
            type="button"
            className="btn-refresh"
            onClick={loadCandidates}
            disabled={loadingCandidates}
          >
            {loadingCandidates ? "Refreshing…" : "Refresh Ladder"}
          </button>
        </div>
        <p className="muted candidates-desc">
          Deterministic 8-stage promotion ladder with fail-closed gates (AGENTS.md §8 &amp; §9).
          Strategies cannot jump stages or trade live without explicit operator verification.
        </p>

        {candidateActionMessage && (
          <div className="alert-message alert-success" role="status">
            {candidateActionMessage}
          </div>
        )}
        {candidateActionError && (
          <div className="alert-message alert-error" role="alert">
            {candidateActionError}
          </div>
        )}

        {candidates.length === 0 ? (
          <p className="muted empty-state">
            No registered strategy candidates yet. Run a backtest above and register it to start the promotion pipeline.
          </p>
        ) : (
          <div className="candidates-list">
            {candidates.map((cand) => {
              const nextStage = getNextStage(cand.stage);
              const stageIndex = PROMOTION_LADDER.indexOf(cand.stage as (typeof PROMOTION_LADDER)[number]);
              return (
                <div key={cand.candidate_id} className="candidate-card">
                  <div className="candidate-header">
                    <div>
                      <h4>{cand.name}</h4>
                      <span className="candidate-meta muted">
                        Version: {cand.version} · Dataset: <code>{cand.dataset_hash.slice(0, 12)}…</code>
                      </span>
                    </div>
                    <span className={`stage-badge stage-${cand.stage.toLowerCase()}`}>
                      {cand.stage}
                    </span>
                  </div>

                  <div className="stage-steps-bar" aria-label="Promotion Progress">
                    {PROMOTION_LADDER.map((stg, i) => (
                      <div
                        key={stg}
                        className={`step-dot ${i <= stageIndex ? "step-active" : ""}`}
                        title={`Stage ${i + 1}: ${stg}`}
                      />
                    ))}
                  </div>

                  <div className="candidate-footer">
                    <div className="candidate-params muted">
                      Params: <code>{JSON.stringify(cand.parameters)}</code>
                    </div>
                    {nextStage ? (
                      <button
                        type="button"
                        className="btn-promote"
                        onClick={() => handlePromoteCandidate(cand)}
                        disabled={promotingId === cand.candidate_id}
                      >
                        {promotingId === cand.candidate_id
                          ? "Evaluating Gate…"
                          : `Promote to ${nextStage}`}
                      </button>
                    ) : (
                      <span className="max-promoted">Fully Promoted (LIVE_BOUNDED)</span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </section>
  );
}
