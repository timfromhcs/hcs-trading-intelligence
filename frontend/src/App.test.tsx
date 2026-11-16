import { describe, it, expect, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";
import { App, screens } from "./App";
import { stubFetch } from "./test-utils";

const routes = {
  "/api/v1/system/status": { mode: "PAPER", execution_available: true, network_enabled: true },
  "/api/v1/system/capabilities": {
    api: true,
    market_data: true,
    portfolio: true,
    paper_orders: true,
    live_orders: false,
    mcp: true,
  },
  "/api/v1/risk/status": { paused: false, changed_at: null },
  "/api/v1/system/config": { host: "127.0.0.1", port: 8765, default_mode: "PAPER" },
  "/api/v1/skills": [],
  "/api/v1/market/candles": [],
  "/api/v1/market/indicators": { contiguous_candles: 0 },
};

describe("App", () => {
  beforeEach(() => {
    window.location.hash = "";
  });

  it("offers a skip link, landmarks and all eleven screens", async () => {
    stubFetch(routes);
    render(<App />);
    expect(screen.getByRole("link", { name: "Skip to main content" })).toHaveAttribute("href", "#main-content");
    expect(screen.getByRole("navigation", { name: "Primary" })).toBeInTheDocument();
    expect(screen.getByRole("main")).toBeInTheDocument();
    expect(screens).toHaveLength(11);
    expect(await screen.findByRole("heading", { level: 1, name: "Overview" })).toBeInTheDocument();
  });

  it("keeps the header while navigating between screens", async () => {
    stubFetch(routes);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/Mode: Paper/);
    await user.click(screen.getByRole("link", { name: "Markets" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Markets" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Markets" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("button", { name: /Kill switch/ })).toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Settings" }));
    expect(await screen.findByText("127.0.0.1")).toBeInTheDocument();
  });

  it("shows an honest unavailable state when the service is down", async () => {
    stubFetch({});
    render(<App />);
    expect(await screen.findByText(/Local service unavailable. Start the application/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Kill switch/ })).toBeInTheDocument();
  });

  it("has no detectable accessibility violations on the overview", async () => {
    stubFetch(routes);
    const { container } = render(<App />);
    await screen.findByText(/Mode: Paper/);
    await screen.findByText("Capabilities");
    expect((await axe(container)).violations).toEqual([]);
  });
});
