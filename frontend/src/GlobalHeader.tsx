import { useCallback, useEffect, useState } from "react";
import {
  getJson,
  postJson,
  requestSignal,
  type Mode,
  type RiskStatus,
  type SystemStatus,
} from "./api";

const modeIcons: Record<Mode, string> = {
  OFFLINE: "○",
  DATA_ONLY: "◇",
  PAPER: "◐",
  LIVE: "●",
};

const modeText: Record<Mode, string> = {
  OFFLINE: "Offline",
  DATA_ONLY: "Data only",
  PAPER: "Paper (simulated)",
  LIVE: "LIVE (real funds)",
};

export function ModeBadge({ mode }: { mode: Mode | null }) {
  if (mode === null) {
    return (
      <span className="mode-badge" data-badge="UNKNOWN">
        <span aria-hidden="true">?</span> Mode unknown
      </span>
    );
  }
  return (
    <span className="mode-badge" data-badge={mode}>
      <span aria-hidden="true">{modeIcons[mode]}</span> <span>Mode: {modeText[mode]}</span>
    </span>
  );
}

export function GlobalHeader({ refreshKey = 0 }: { refreshKey?: number }) {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [risk, setRisk] = useState<RiskStatus | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirmResume, setConfirmResume] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (controller: AbortController) => {
    const signal = requestSignal(controller);
    const [s, r] = await Promise.allSettled([
      getJson<SystemStatus>("/api/v1/system/status", signal),
      getJson<RiskStatus>("/api/v1/risk/status", signal),
    ]);
    if (controller.signal.aborted) return;
    setStatus(s.status === "fulfilled" ? s.value : null);
    setRisk(r.status === "fulfilled" ? r.value : null);
    setUnavailable(s.status === "rejected");
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller);
    const timer = setInterval(() => void load(controller), 15_000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [load, refreshKey]);

  async function toggle(pause: boolean) {
    setBusy(true);
    setError(null);
    try {
      const updated = await postJson<RiskStatus>(
        pause ? "/api/v1/risk/pause" : "/api/v1/risk/resume",
      );
      setRisk(updated);
      setConfirmResume(false);
    } catch {
      setError(
        pause
          ? "Kill switch request failed. Execution state is unconfirmed."
          : "Resume request failed. Execution stays paused.",
      );
    } finally {
      setBusy(false);
    }
  }

  const paused = risk?.paused ?? null;

  return (
    <header className="site-header global-header" data-live={status?.mode === "LIVE"}>
      <div className="header-inner">
        <span className="brand">HCS<span>.media</span></span>
        <ul className="header-status" aria-label="System state">
          <li><ModeBadge mode={status?.mode ?? null} /></li>
          <li>
            Network:{" "}
            <strong>
              {status === null ? "unknown" : status.network_enabled ? "external enabled" : "local only"}
            </strong>
          </li>
          <li>
            Execution:{" "}
            <strong className={paused ? "quote-warning" : ""}>
              {paused === null ? "unknown" : paused ? "PAUSED" : "clear"}
            </strong>
          </li>
        </ul>
        <div className="kill-switch">
          {paused === true ? (
            confirmResume ? (
              <>
                <button type="button" className="btn-action btn-resume" disabled={busy} onClick={() => void toggle(false)}>
                  Confirm resume
                </button>
                <button type="button" onClick={() => setConfirmResume(false)}>Cancel</button>
              </>
            ) : (
              <button type="button" className="btn-action btn-resume" disabled={busy} onClick={() => setConfirmResume(true)}>
                Resume execution
              </button>
            )
          ) : (
            <button type="button" className="btn-action btn-danger" disabled={busy} onClick={() => void toggle(true)}>
              Kill switch: pause all execution
            </button>
          )}
        </div>
      </div>
      {unavailable && (
        <p role="alert" className="header-banner">
          Local service unavailable. Displayed state may be outdated; do not assume execution is paused or clear.
        </p>
      )}
      {error !== null && <p role="alert" className="header-banner">{error}</p>}
    </header>
  );
}
