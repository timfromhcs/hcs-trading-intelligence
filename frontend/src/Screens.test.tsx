import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Capabilities, SystemStatus } from "./api";
import { Overview } from "./Screens";

const status: SystemStatus = {
  mode: "PAPER",
  execution_available: false,
  network_enabled: false,
};
const capabilities: Capabilities = {
  api: false,
  market_data: false,
  portfolio: false,
  paper_orders: false,
  live_orders: false,
  mcp: false,
};

describe("Overview browser runtime", () => {
  it("shows WebGPU and WebAssembly state without authorizing actions", () => {
    vi.stubGlobal("navigator", { gpu: {} });
    vi.stubGlobal("WebAssembly", { validate: () => true });
    render(<Overview status={status} capabilities={capabilities} />);
    expect(
      screen.getByRole("heading", { name: "Browser runtime" }),
    ).toBeInTheDocument();
    expect(screen.getByText("WebGPU")).toBeInTheDocument();
    expect(screen.getByText("WebAssembly")).toBeInTheDocument();
    expect(
      screen.getByText(/never authorize an order/i),
    ).toBeInTheDocument();
  });
});
