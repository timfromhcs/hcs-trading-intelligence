import { useCallback, useEffect, useState, type ReactNode } from "react";
import {
  getJson,
  requestSignal,
  type Capabilities,
  type SystemStatus,
} from "./api";
import { MarketChart } from "./CandleChart";
import { GlobalHeader } from "./GlobalHeader";
import { readLocale, saveLocale, t, type LabelKey, type Locale } from "./i18n";
import { NewsTimeline } from "./NewsTimeline";
import { OrderPreview } from "./OrderPreview";
import { PaperPortfolio } from "./PaperPortfolio";
import { RiskControls } from "./RiskControls";
import { Agents, ApiMcp, Overview, Settings } from "./Screens";
import { SpotOrders } from "./SpotOrders";
import { StrategyLab } from "./StrategyLab";
import { WalletSnapshots } from "./WalletSnapshots";

export const screens = [
  { id: "overview", label: "Overview" },
  { id: "markets", label: "Markets" },
  { id: "world", label: "World Monitor" },
  { id: "portfolio", label: "Portfolio" },
  { id: "wallets", label: "Wallets" },
  { id: "strategy", label: "Strategy Lab" },
  { id: "risk", label: "Risk Center" },
  { id: "ledger", label: "Execution Ledger" },
  { id: "agents", label: "Agents" },
  { id: "api", label: "API & MCP" },
  { id: "settings", label: "Settings" },
] as const;

type ScreenId = (typeof screens)[number]["id"];

function screenFromHash(): ScreenId {
  const id = window.location.hash.replace(/^#\/?/, "");
  return screens.find((s) => s.id === id)?.id ?? "overview";
}

type Snapshot =
  | { state: "loading" | "unavailable" }
  | { state: "ready"; status: SystemStatus; capabilities: Capabilities };

export function App() {
  const [screen, setScreen] = useState<ScreenId>(screenFromHash);
  const [snapshot, setSnapshot] = useState<Snapshot>({ state: "loading" });
  const [refresh, setRefresh] = useState(0);
  const [locale, setLocale] = useState<Locale>(readLocale);

  useEffect(() => {
    const onHash = () => setScreen(screenFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const signal = requestSignal(controller);
    Promise.all([
      getJson<SystemStatus>("/api/v1/system/status", signal),
      getJson<Capabilities>("/api/v1/system/capabilities", signal),
    ]).then(
      ([status, capabilities]) => setSnapshot({ state: "ready", status, capabilities }),
      () => {
        if (!controller.signal.aborted) setSnapshot({ state: "unavailable" });
      },
    );
    return () => controller.abort();
  }, [refresh]);

  const navigate = useCallback((id: ScreenId) => {
    window.location.hash = `/${id}`;
    setScreen(id);
  }, []);

  const ready = snapshot.state === "ready" ? snapshot : null;
  let content: ReactNode;
  if (ready === null) {
    content = (
      <section className="panel" aria-labelledby="status-heading">
        <div className="section-heading">
          <h2 id="status-heading">System status</h2>
          <button
            type="button"
            disabled={snapshot.state === "loading"}
            onClick={() => {
              setSnapshot({ state: "loading" });
              setRefresh((r) => r + 1);
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
        </div>
      </section>
    );
  } else {
    switch (screen) {
      case "overview":
        content = <Overview status={ready.status} capabilities={ready.capabilities} />;
        break;
      case "markets":
        content = ready.capabilities.market_data ? (
          <MarketChart />
        ) : (
          <section className="panel"><h2>Markets</h2><p className="muted">Market data is not enabled on this installation.</p></section>
        );
        break;
      case "world":
        content = (
          <>
            <NewsTimeline />
            <section className="panel" aria-labelledby="world-note">
              <h2 id="world-note">Coverage</h2>
              <p className="muted">
                Only official central-bank releases are ingested today. Politics, regulation
                and crypto news categories, source counts and uncertainty are not available.
              </p>
            </section>
          </>
        );
        break;
      case "portfolio":
        content = ready.capabilities.portfolio ? <PaperPortfolio /> : (
          <section className="panel"><h2>Portfolio</h2><p className="muted">Portfolio is not enabled.</p></section>
        );
        break;
      case "wallets":
        content = <WalletSnapshots />;
        break;
      case "strategy":
        content = <StrategyLab />;
        break;
      case "risk":
        content = <RiskControls />;
        break;
      case "ledger":
        content = (
          <>
            <OrderPreview liveAvailable={ready.capabilities.live_orders} />
            <SpotOrders />
          </>
        );
        break;
      case "agents":
        content = <Agents />;
        break;
      case "api":
        content = <ApiMcp capabilities={ready.capabilities} />;
        break;
      case "settings":
        content = <Settings />;
        break;
    }
  }

  return (
    <>
      <a className="skip-link" href="#main-content">Skip to main content</a>
      <GlobalHeader
        refreshKey={refresh}
        locale={locale}
        onLocaleChange={(next) => {
          setLocale(next);
          saveLocale(next);
        }}
      />
      <nav aria-label="Primary" className="primary-nav">
        <ul>
          {screens.map((s) => (
            <li key={s.id}>
              <a
                href={`#/${s.id}`}
                aria-current={screen === s.id ? "page" : undefined}
                onClick={(event) => {
                  event.preventDefault();
                  navigate(s.id);
                }}
              >
                {t(locale, s.id as LabelKey)}
              </a>
            </li>
          ))}
        </ul>
      </nav>
      <main id="main-content" tabIndex={-1}>
        <h1>{t(locale, screen as LabelKey)}</h1>
        {content}
      </main>
    </>
  );
}
