import { useEffect, useState } from "react";
import { getJson, requestSignal, type Capabilities, type SystemStatus } from "./api";
import { AuditLog } from "./AuditLog";
import { detectRuntimeCapabilities } from "./capabilities";
import { DataSources } from "./DataSources";
import { MarketQuote } from "./MarketQuote";
import { ResearchRuns } from "./ResearchRuns";

export const capabilityLabels: Record<keyof Capabilities, string> = {
  api: "Local API",
  market_data: "Market data",
  portfolio: "Paper portfolio",
  paper_orders: "Paper orders",
  live_orders: "Live orders",
  mcp: "MCP",
  wallet_snapshots: "Wallet snapshots",
};

function useLocal<T>(path: string): { state: "loading" | "unavailable" } | { state: "ready"; data: T } {
  const [result, setResult] = useState<
    { state: "loading" | "unavailable" } | { state: "ready"; data: T }
  >({ state: "loading" });
  useEffect(() => {
    const controller = new AbortController();
    getJson<T>(path, requestSignal(controller)).then(
      (data) => {
        if (!controller.signal.aborted) setResult({ state: "ready", data });
      },
      () => {
        if (!controller.signal.aborted) setResult({ state: "unavailable" });
      },
    );
    return () => controller.abort();
  }, [path]);
  return result;
}

function Unavailable({ children }: { children: string }) {
  return <p className="muted">Unavailable: {children}</p>;
}

export function CapabilityList({ capabilities }: { capabilities: Capabilities }) {
  const names = Object.keys(capabilityLabels) as (keyof Capabilities)[];
  return (
    <dl className="capabilities">
      {names
        .filter((name) => capabilities[name] !== undefined)
        .map((name) => (
          <div key={name}>
            <dt>{capabilityLabels[name]}</dt>
            <dd className={capabilities[name] ? "available" : "muted"}>
              {capabilities[name] ? "Available" : "Unavailable"}
            </dd>
          </div>
        ))}
    </dl>
  );
}

export function Overview({
  status,
  capabilities,
}: {
  status: SystemStatus;
  capabilities: Capabilities;
}) {
  const runtime = detectRuntimeCapabilities();
  return (
    <>
      <section className="panel" aria-labelledby="status-heading">
        <h2 id="status-heading">System status</h2>
        <dl className="status-grid">
          <div><dt>Mode</dt><dd data-mode={status.mode}>{status.mode}</dd></div>
          <div><dt>External network</dt><dd>{status.network_enabled ? "Enabled" : "Disabled"}</dd></div>
          <div><dt>Execution</dt><dd>{status.execution_available ? "Available" : "Unavailable"}</dd></div>
        </dl>
      </section>
      <section className="panel" aria-labelledby="equity-heading">
        <h2 id="equity-heading">Equity and drawdown</h2>
        <Unavailable>
          portfolio equity, day P&amp;L and drawdown are not exposed by the service yet. Hot
          and cold balances are not combined: the cold reserve is never loaded into this
          application.
        </Unavailable>
      </section>
      {capabilities.market_data && <MarketQuote />}
      <DataSources />
      <section className="panel" aria-labelledby="runtime-heading">
        <h2 id="runtime-heading">Browser runtime</h2>
        <dl className="status-grid">
          <div><dt>WebGPU</dt><dd>{runtime.webgpu ? "Available" : "Unavailable"}</dd></div>
          <div><dt>WebAssembly</dt><dd>{runtime.webassembly ? "Available" : "Unavailable"}</dd></div>
          <div>
            <dt>SharedArrayBuffer</dt>
            <dd>{runtime.sharedArrayBuffer ? "Available" : "Unavailable"}</dd>
          </div>
        </dl>
        <p className="muted">
          Informational only. Browser capabilities never authorize an order or
          change a risk limit.
        </p>
      </section>
      <section className="panel" aria-labelledby="capabilities-heading">
        <h2 id="capabilities-heading">Capabilities</h2>
        <CapabilityList capabilities={capabilities} />
      </section>
    </>
  );
}

interface Skill {
  name: string;
  version: string;
  purpose: string;
  capability_tier: string;
  required_tools: string[];
}

export function Agents() {
  const skills = useLocal<Skill[]>("/api/v1/skills");
  return (
    <>
      <section className="panel" aria-labelledby="skills-heading">
        <h2 id="skills-heading">Agent skills</h2>
        <p className="muted">
          Skills describe how an agent uses permitted tools. They grant no permissions. Live
          agent state and last-cycle timing are not exposed by the service.
        </p>
        <div role="status">
          {skills.state === "loading" && <p>Loading skills…</p>}
          {skills.state === "unavailable" && <p className="quote-warning">Skill list unavailable.</p>}
        </div>
        {skills.state === "ready" &&
          (skills.data.length === 0 ? (
            <p className="muted">No skills registered.</p>
          ) : (
            <ul className="news-list">
              {skills.data.map((skill) => (
                <li key={skill.name}>
                  <h3>{skill.name} <span className="muted">v{skill.version}</span></h3>
                  <p>{skill.purpose}</p>
                  <p>Tier: {skill.capability_tier} · Tools: {skill.required_tools.join(", ") || "none"}</p>
                </li>
              ))}
            </ul>
          ))}
      </section>
      <ResearchRuns />
      <AuditLog />
    </>
  );
}

export function ApiMcp({ capabilities }: { capabilities: Capabilities | null }) {
  const config = useLocal<{ host: string; port: number }>("/api/v1/system/config");
  return (
    <section className="panel" aria-labelledby="api-heading">
      <h2 id="api-heading">API and MCP</h2>
      <div role="status">
        {config.state === "loading" && <p>Loading endpoint configuration…</p>}
        {config.state === "unavailable" && <p className="quote-warning">Endpoint configuration unavailable.</p>}
      </div>
      <dl className="status-grid">
        {config.state === "ready" && (
          <div><dt>REST (loopback only)</dt><dd>{config.data.host}:{config.data.port}</dd></div>
        )}
        <div><dt>OpenAPI schema</dt><dd><a href="/openapi.json">/openapi.json</a></dd></div>
        <div>
          <dt>MCP</dt>
          <dd>{capabilities === null ? "Unknown" : capabilities.mcp ? "Available (stdio)" : "Unavailable"}</dd>
        </div>
      </dl>
      <Unavailable>
        connected-client listing, per-client scopes and client revocation are not exposed by
        the service. MCP tool calls appear in the audit log on the Agents screen.
      </Unavailable>
    </section>
  );
}

export function Settings() {
  const config = useLocal<{ host: string; port: number; default_mode: string }>(
    "/api/v1/system/config",
  );
  return (
    <section className="panel" aria-labelledby="settings-heading">
      <h2 id="settings-heading">Settings</h2>
      <div role="status">
        {config.state === "loading" && <p>Loading configuration…</p>}
        {config.state === "unavailable" && <p className="quote-warning">Configuration unavailable.</p>}
      </div>
      {config.state === "ready" && (
        <dl className="status-grid">
          <div><dt>Bind address</dt><dd>{config.data.host}</dd></div>
          <div><dt>Port</dt><dd>{config.data.port}</dd></div>
          <div><dt>Default mode</dt><dd>{config.data.default_mode}</dd></div>
        </dl>
      )}
      <Unavailable>
        provider and API-key status, model path, retention, backup and network allowlists
        are managed through local configuration and are not exposed by the service. Secrets
        are never shown in this interface.
      </Unavailable>
    </section>
  );
}
