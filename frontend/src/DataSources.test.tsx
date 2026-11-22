import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { axe } from "jest-axe";
import { DataSources } from "./DataSources";
import { stubFetch } from "./test-utils";

const row = {
  kind: "macro",
  last_success: "2026-01-01T12:00:00Z",
  last_error: null,
  consecutive_failures: 0,
  enabled: true,
  requires_api_key: false,
  breaker_state: "CLOSED",
  retry_in_seconds: null,
};

const body = {
  ingestion_enabled: true,
  ingestion_mode: "DATA_ONLY",
  sources: [
    { ...row, source_id: "ecb-sdmx", quality: "FRESH" },
    {
      ...row,
      source_id: "federal-reserve-monetary-policy",
      kind: "news",
      quality: "DEGRADED",
      last_error: "MacroRateLimited",
      breaker_state: "OPEN",
      retry_in_seconds: 41.2,
    },
    {
      ...row,
      source_id: "fred",
      quality: "MISSING",
      enabled: false,
      requires_api_key: true,
      last_success: null,
    },
  ],
};

describe("DataSources", () => {
  it("shows freshness, breaker state and retry timing as text", async () => {
    stubFetch({ "/api/v1/data/sources": body });
    const { container } = render(<DataSources />);
    expect(await screen.findByText("ecb-sdmx")).toBeInTheDocument();
    expect(screen.getByText("Fresh")).toBeInTheDocument();
    expect(screen.getByText(/Degraded · MacroRateLimited/)).toBeInTheDocument();
    expect(screen.getByText("OPEN")).toBeInTheDocument();
    expect(screen.getByText("42 s")).toBeInTheDocument();
    expect(screen.getByText("Disabled (API key required)")).toBeInTheDocument();
    expect(screen.getByText(/Background ingestion is on \(DATA_ONLY\)/)).toBeInTheDocument();
    expect((await axe(container)).violations).toEqual([]);
  });

  it("says when ingestion is off and when the service is unavailable", async () => {
    stubFetch({
      "/api/v1/data/sources": { ...body, ingestion_enabled: false, ingestion_mode: null },
    });
    const { unmount } = render(<DataSources />);
    expect(await screen.findByText(/ingestion is off/)).toBeInTheDocument();
    unmount();
    stubFetch({});
    render(<DataSources />);
    expect(await screen.findByText("Source status unavailable.")).toBeInTheDocument();
  });
});
