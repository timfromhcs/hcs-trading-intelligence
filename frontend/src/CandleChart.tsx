import { useEffect, useState } from "react";
import { getJson, requestSignal } from "./api";

export interface Candle {
  opened_at: string;
  closed_at: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
}

export interface IndicatorValues {
  contiguous_candles: number;
  rsi14: string | null;
  sma20: string | null;
  ema20: string | null;
  macd: string | null;
  macd_signal: string | null;
  macd_histogram: string | null;
  atr14: string | null;
  bollinger_upper: string | null;
  bollinger_lower: string | null;
}

const WIDTH = 720;
const PRICE_HEIGHT = 240;
const VOLUME_HEIGHT = 50;
const GAP = 10;
const HEIGHT = PRICE_HEIGHT + GAP + VOLUME_HEIGHT;

/** Plot-only numeric conversion; displayed values always stay the API strings. */
const n = (value: string): number => Number(value);

export function CandleChart({
  candles,
  indicators,
}: {
  candles: Candle[];
  indicators: IndicatorValues | null;
}) {
  if (candles.length === 0) {
    return <p className="muted">No candles stored for this interval yet.</p>;
  }
  const min = Math.min(...candles.map((c) => n(c.low)));
  const max = Math.max(...candles.map((c) => n(c.high)));
  const span = max - min || 1;
  const maxVolume = Math.max(...candles.map((c) => n(c.volume))) || 1;
  const step = WIDTH / candles.length;
  const body = Math.max(1, step * 0.6);
  const y = (price: number) => PRICE_HEIGHT - ((price - min) / span) * PRICE_HEIGHT;

  const overlays: { label: string; value: string | null; cls: string }[] = indicators
    ? [
        { label: "SMA20", value: indicators.sma20, cls: "ov-sma" },
        { label: "EMA20", value: indicators.ema20, cls: "ov-ema" },
        { label: "Bollinger upper", value: indicators.bollinger_upper, cls: "ov-band" },
        { label: "Bollinger lower", value: indicators.bollinger_lower, cls: "ov-band" },
      ]
    : [];

  const first = candles[0];
  const last = candles[candles.length - 1];
  const description =
    `Candlestick chart of ${candles.length} candles from ${first.opened_at} to ${last.closed_at}. ` +
    `Last close ${last.close}, range ${min} to ${max}.`;

  const indicatorRows: [string, string | null][] = indicators
    ? [
        ["RSI(14)", indicators.rsi14],
        ["MACD", indicators.macd],
        ["MACD signal", indicators.macd_signal],
        ["MACD histogram", indicators.macd_histogram],
        ["ATR(14)", indicators.atr14],
        ["SMA(20)", indicators.sma20],
        ["EMA(20)", indicators.ema20],
        ["Bollinger upper", indicators.bollinger_upper],
        ["Bollinger lower", indicators.bollinger_lower],
      ]
    : [];

  return (
    <figure className="candle-figure">
      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label={description} className="candle-chart">
        {candles.map((c, i) => {
          const x = i * step + step / 2;
          const up = n(c.close) >= n(c.open);
          const top = y(Math.max(n(c.open), n(c.close)));
          const bottom = y(Math.min(n(c.open), n(c.close)));
          const volumeHeight = (n(c.volume) / maxVolume) * VOLUME_HEIGHT;
          return (
            <g key={c.opened_at} className={up ? "candle-up" : "candle-down"} data-testid="candle">
              <line x1={x} x2={x} y1={y(n(c.high))} y2={y(n(c.low))} />
              <rect x={x - body / 2} y={top} width={body} height={Math.max(1, bottom - top)} />
              <rect
                className="candle-volume"
                x={x - body / 2}
                y={HEIGHT - volumeHeight}
                width={body}
                height={volumeHeight}
              />
            </g>
          );
        })}
        {overlays.map((o) =>
          o.value !== null && n(o.value) >= min && n(o.value) <= max ? (
            <line
              key={o.label}
              className={`overlay-line ${o.cls}`}
              x1={0}
              x2={WIDTH}
              y1={y(n(o.value))}
              y2={y(n(o.value))}
            />
          ) : null,
        )}
      </svg>
      <figcaption className="muted">
        Candles and volume from stored provider data. Overlay lines mark the latest indicator
        values only, not a history.
      </figcaption>
      {indicators !== null && (
        <dl className="status-grid indicator-grid">
          {indicatorRows.map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>{value ?? "Warm-up incomplete"}</dd>
            </div>
          ))}
        </dl>
      )}
      <details className="chart-table">
        <summary>Data table (last {Math.min(candles.length, 20)} candles)</summary>
        <div className="research-runs-scroll">
          <table>
            <caption className="visually-hidden">Recent candles</caption>
            <thead>
              <tr>
                <th scope="col">Opened</th>
                <th scope="col">Open</th>
                <th scope="col">High</th>
                <th scope="col">Low</th>
                <th scope="col">Close</th>
                <th scope="col">Volume</th>
              </tr>
            </thead>
            <tbody>
              {candles.slice(-20).map((c) => (
                <tr key={c.opened_at}>
                  <td><time dateTime={c.opened_at}>{new Date(c.opened_at).toLocaleString()}</time></td>
                  <td>{c.open}</td>
                  <td>{c.high}</td>
                  <td>{c.low}</td>
                  <td>{c.close}</td>
                  <td>{c.volume}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}

const intervals = [
  { value: 1, label: "1 minute" },
  { value: 5, label: "5 minutes" },
  { value: 15, label: "15 minutes" },
  { value: 60, label: "1 hour" },
  { value: 240, label: "4 hours" },
  { value: 1440, label: "1 day" },
];

type ChartState =
  | { state: "loading" | "unavailable" }
  | { state: "ready"; candles: Candle[]; indicators: IndicatorValues | null };

export function MarketChart() {
  const [interval, setIntervalMinutes] = useState(60);
  const [chart, setChart] = useState<ChartState>({ state: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    const signal = requestSignal(controller, 8000);
    setChart({ state: "loading" });
    Promise.all([
      getJson<Candle[]>(`/api/v1/market/candles?interval=${interval}&limit=120`, signal),
      getJson<IndicatorValues>(`/api/v1/market/indicators?interval=${interval}`, signal).catch(
        () => null,
      ),
    ]).then(
      ([candles, indicators]) => {
        if (!controller.signal.aborted) setChart({ state: "ready", candles, indicators });
      },
      () => {
        if (!controller.signal.aborted) setChart({ state: "unavailable" });
      },
    );
    return () => controller.abort();
  }, [interval]);

  return (
    <section className="panel" aria-labelledby="chart-heading">
      <div className="section-heading">
        <h2 id="chart-heading">BTC/USD candles</h2>
        <label>
          Interval{" "}
          <select value={interval} onChange={(e) => setIntervalMinutes(Number(e.target.value))}>
            {intervals.map((i) => (
              <option key={i.value} value={i.value}>{i.label}</option>
            ))}
          </select>
        </label>
      </div>
      <div role="status">
        {chart.state === "loading" && <p>Loading candles…</p>}
        {chart.state === "unavailable" && <p className="quote-warning">Candle data unavailable.</p>}
      </div>
      {chart.state === "ready" && <CandleChart candles={chart.candles} indicators={chart.indicators} />}
    </section>
  );
}
