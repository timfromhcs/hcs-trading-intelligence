import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";
import { multiplyDecimals } from "./decimal";
import { LIVE_CONFIRMATION, OrderPreview } from "./OrderPreview";

describe("decimal math", () => {
  it("multiplies exactly without floating point error", () => {
    expect(multiplyDecimals("0.1", "0.2")).toBe("0.02");
    expect(multiplyDecimals("0.00000001", "65000.5")).toBe("0.000650005");
    expect(multiplyDecimals("2", "3")).toBe("6");
    expect(multiplyDecimals("-1", "3")).toBeNull();
  });
});

describe("OrderPreview", () => {
  it("shows disclosure fields and exact notional", async () => {
    const user = userEvent.setup();
    render(<OrderPreview liveAvailable={false} />);
    await user.type(screen.getByLabelText("Amount (BTC)"), "0.1");
    await user.type(screen.getByLabelText("Limit price (USD)"), "0.2");
    await user.click(screen.getByRole("button", { name: "Preview" }));
    expect(screen.getByText("Notional")).toBeInTheDocument();
    expect(screen.getByText("0.02 USD")).toBeInTheDocument();
    expect(screen.getAllByText("Not provided by service")).toHaveLength(4);
  });

  it("rejects invalid input", async () => {
    const user = userEvent.setup();
    render(<OrderPreview liveAvailable={false} />);
    await user.type(screen.getByLabelText("Amount (BTC)"), "abc");
    await user.click(screen.getByRole("button", { name: "Preview" }));
    expect(screen.getByRole("alert")).toHaveTextContent(/positive decimal/);
  });

  it("keeps live activation disabled without a live route even after typed confirmation", async () => {
    const user = userEvent.setup();
    render(<OrderPreview liveAvailable={false} />);
    await user.type(screen.getByLabelText(/to acknowledge/), LIVE_CONFIRMATION);
    expect(screen.getByRole("button", { name: "Enable live trading" })).toBeDisabled();
  });

  it("requires the exact typed confirmation when a live route exists", async () => {
    const user = userEvent.setup();
    render(<OrderPreview liveAvailable />);
    const button = screen.getByRole("button", { name: "Enable live trading" });
    expect(button).toBeDisabled();
    await user.type(screen.getByLabelText(/to acknowledge/), "enable live trading");
    expect(button).toBeDisabled();
    await user.clear(screen.getByLabelText(/to acknowledge/));
    await user.type(screen.getByLabelText(/to acknowledge/), LIVE_CONFIRMATION);
    expect(button).toBeEnabled();
  });

  it("has no detectable accessibility violations", async () => {
    const { container } = render(<OrderPreview liveAvailable={false} />);
    expect((await axe(container)).violations).toEqual([]);
  });
});
