import { describe, it, expect } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";
import { GlobalHeader } from "./GlobalHeader";
import { stubFetch } from "./test-utils";

const base = {
  "/api/v1/system/status": { mode: "PAPER", execution_available: true, network_enabled: true },
  "/api/v1/risk/status": { paused: false, changed_at: null },
};

describe("GlobalHeader", () => {
  it("shows PAPER mode with text, network identity and kill switch", async () => {
    stubFetch(base);
    render(<GlobalHeader />);
    expect(await screen.findByText(/Mode: Paper/)).toBeInTheDocument();
    expect(screen.getByText("external enabled")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Kill switch/ })).toBeEnabled();
  });

  it("styles LIVE distinctly", async () => {
    stubFetch({ ...base, "/api/v1/system/status": { ...base["/api/v1/system/status"], mode: "LIVE" } });
    const { container } = render(<GlobalHeader />);
    expect(await screen.findByText(/LIVE \(real funds\)/)).toBeInTheDocument();
    expect(container.querySelector("[data-badge='LIVE']")).not.toBeNull();
    expect(container.querySelector("header")?.getAttribute("data-live")).toBe("true");
  });

  it("stays visible and warns when the service is unavailable", async () => {
    stubFetch({});
    render(<GlobalHeader />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable/i);
    expect(screen.getByText("Mode unknown")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Kill switch/ })).toBeInTheDocument();
  });

  it("posts pause and then requires confirmation to resume", async () => {
    const calls = stubFetch({
      ...base,
      "/api/v1/risk/pause": { paused: true, changed_at: "2026-01-01T00:00:00Z" },
      "/api/v1/risk/resume": { paused: false, changed_at: "2026-01-01T00:01:00Z" },
    });
    const user = userEvent.setup();
    render(<GlobalHeader />);
    await user.click(await screen.findByRole("button", { name: /Kill switch/ }));
    await screen.findByRole("button", { name: "Resume execution" });
    expect(calls.some((c) => c.path === "/api/v1/risk/pause" && c.method === "POST")).toBe(true);

    await user.click(screen.getByRole("button", { name: "Resume execution" }));
    expect(calls.some((c) => c.path === "/api/v1/risk/resume")).toBe(false);
    await user.click(screen.getByRole("button", { name: "Confirm resume" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v1/risk/resume")).toBe(true));
  });

  it("reports a failed kill switch request", async () => {
    stubFetch(base);
    const user = userEvent.setup();
    render(<GlobalHeader />);
    await user.click(await screen.findByRole("button", { name: /Kill switch/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/unconfirmed/);
  });

  it("has no detectable accessibility violations", async () => {
    stubFetch(base);
    const { container } = render(<GlobalHeader />);
    await screen.findByText(/Mode: Paper/);
    expect((await axe(container)).violations).toEqual([]);
  });
});
