import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { MarketQuote } from "./MarketQuote";
import "./styles.css";

interface SystemStatus {
  mode: "OFFLINE" | "DATA_ONLY" | "PAPER" | "LIVE";
  execution_available: boolean;
  network_enabled: boolean;
}

interface Capabilities {
  api: boolean;
  market_data: boolean;
  portfolio: boolean;
  paper_orders: boolean;
  live_orders: boolean;
  mcp: boolean;
}

const capabilityLabels: Record<keyof Capabilities, string> = {
  api: "Local API",
  market_data: "Market data",
  portfolio: "Portfolio",
  paper_orders: "Paper orders",
  live_orders: "Live orders",
  mcp: "MCP",
};

const capabilityNames = Object.keys(capabilityLabels) as (keyof Capabilities)[];

type Snapshot =
  | { state: "loading" | "unavailable" }
  | { state: "ready"; status: SystemStatus; capabilities: Capabilities };

async function readJson<T>(path: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(path, { signal, cache: "no-store" });
  if (!response.ok) throw new Error("Local service unavailable");
  return (await response.json()) as T;
}

function App() {
  const [snapshot, setSnapshot] = useState<Snapshot>({ state: "loading" });
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(5000)]);
    Promise.all([
      readJson<SystemStatus>("/api/v1/system/status", signal),
      readJson<Capabilities>("/api/v1/system/capabilities", signal),
    ]).then(
      ([status, capabilities]) => setSnapshot({ state: "ready", status, capabilities }),
      () => {
        if (!controller.signal.aborted) setSnapshot({ state: "unavailable" });
      },
    );
    return () => controller.abort();
  }, [refresh]);

  return (
    <>
      <header className="site-header">
        <div className="header-inner">
          <span className="brand">HCS<span>.media</span></span>
          <a href="/openapi.json">OpenAPI schema</a>
        </div>
      </header>
      <main>
        <h1>Trading Intelligence</h1>
        <section className="panel" aria-labelledby="status-heading">
          <div className="section-heading">
            <h2 id="status-heading">System status</h2>
            <button
              type="button"
              disabled={snapshot.state === "loading"}
              onClick={() => {
                setSnapshot({ state: "loading" });
                setRefresh((revision) => revision + 1);
              }}
            >
              Refresh
            </button>
          </div>
          <div role="status">
            {snapshot.state === "loading" && <p>Checking local service…</p>}
            {snapshot.state === "unavailable" && (
              <p>Local service unavailable. Start the application and refresh.</p>
            )}
            {snapshot.state === "ready" && (
              <dl className="status-grid">
                <div><dt>Mode</dt><dd data-mode={snapshot.status.mode}>{snapshot.status.mode}</dd></div>
                <div><dt>External network</dt><dd>{snapshot.status.network_enabled ? "Enabled" : "Disabled"}</dd></div>
                <div><dt>Execution</dt><dd>{snapshot.status.execution_available ? "Available" : "Unavailable"}</dd></div>
              </dl>
            )}
          </div>
        </section>
        {snapshot.state === "ready" && snapshot.capabilities.market_data && <MarketQuote />}
        {snapshot.state === "ready" && (
          <section className="panel" aria-labelledby="capabilities-heading">
            <h2 id="capabilities-heading">Capabilities</h2>
            <dl className="capabilities">
              {capabilityNames.map((name) => (
                <div key={name}>
                  <dt>{capabilityLabels[name]}</dt>
                  <dd className={snapshot.capabilities[name] ? "available" : "muted"}>
                    {snapshot.capabilities[name] ? "Available" : "Unavailable"}
                  </dd>
                </div>
              ))}
            </dl>
          </section>
        )}
      </main>
    </>
  );
}

const root = document.getElementById("root");
if (root === null) throw new Error("Application root is missing");
createRoot(root).render(<App />);
