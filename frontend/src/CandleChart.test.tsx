import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { axe } from "jest-axe";
import { CandleChart, MarketChart, type Candle, type IndicatorValues } from "./CandleChart";
import { stubFetch } from "./test-utils";

const candles: Candle[] = [0, 1, 2].map((i) => ({
  opened_at: `2026-01-01T0${i}:00:00Z`,
  closed_at: `2026-01-01T0${i + 1}:00:00Z`,
  open: String(100 + i),
  high: String(105 + i),
  low: String(95 + i),
  close: String(102 + i),
  volume: String(10 + i),
}));

const indicators: IndicatorValues = {
  contiguous_candles: 3,
  rsi14: null,
  sma20: "101.5",
  ema20: "102",
  macd: null,
  macd_signal: null,
  macd_histogram: null,
  atr14: "4.2",
  bollinger_upper: "106",
  bollinger_lower: "96",
};

describe("CandleChart", () => {
  it("renders one group per candle with an accessible description and data table", () => {
    render(<CandleChart candles={candles} indicators={indicators} />);
    expect(screen.getAllByTestId("candle")).toHaveLength(3);
    expect(screen.getByRole("img", { name: /3 candles.*Last close 104/ })).toBeInTheDocument();
    expect(screen.getByText("Data table (last 3 candles)")).toBeInTheDocument();
    expect(screen.getByRole("table", { hidden: true })).toBeInTheDocument();
  });

  it("marks incomplete indicators as warm-up instead of inventing values", () => {
    render(<CandleChart candles={candles} indicators={indicators} />);
    expect(screen.getAllByText("Warm-up incomplete").length).toBe(4);
    expect(screen.getByText("4.2")).toBeInTheDocument();
  });

  it("shows an honest empty state", () => {
    render(<CandleChart candles={[]} indicators={null} />);
    expect(screen.getByText(/No candles stored/)).toBeInTheDocument();
  });

  it("has no detectable accessibility violations", async () => {
    const { container } = render(<CandleChart candles={candles} indicators={indicators} />);
    expect((await axe(container)).violations).toEqual([]);
  });
});

describe("MarketChart", () => {
  it("loads candles and indicators from the existing endpoints", async () => {
    const calls = stubFetch({
      "/api/v1/market/candles": candles,
      "/api/v1/market/indicators": indicators,
    });
    render(<MarketChart />);
    expect(await screen.findAllByTestId("candle")).toHaveLength(3);
    expect(calls.map((c) => c.path)).toContain("/api/v1/market/candles?interval=60&limit=120");
  });

  it("reports unavailable data", async () => {
    stubFetch({});
    render(<MarketChart />);
    expect(await screen.findByText("Candle data unavailable.")).toBeInTheDocument();
  });
});
