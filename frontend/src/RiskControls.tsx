import { useEffect, useState } from "react";

interface RiskStatus {
  paused: boolean;
  changed_at: string | null;
}

type PauseState =
  | { state: "loading" }
  | { state: "unavailable" }
  | { state: "ready"; status: RiskStatus };

const timeFormat = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

async function readRiskStatus(signal: AbortSignal): Promise<RiskStatus> {
  const response = await fetch("/api/v1/risk/status", {
    signal,
    cache: "no-store",
  });
  if (!response.ok) throw new Error("Risk status unavailable");
  return (await response.json()) as RiskStatus;
}

export function RiskControls() {
  const [pauseState, setPauseState] = useState<PauseState>({ state: "loading" });
  const [submitting, setSubmitting] = useState(false);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(5000)]);
    readRiskStatus(signal).then(
      (status) => {
        if (!controller.signal.aborted) setPauseState({ state: "ready", status });
      },
      () => {
        if (!controller.signal.aborted) setPauseState({ state: "unavailable" });
      },
    );
    return () => controller.abort();
  }, [refresh]);

  async function handleTogglePause(currentPaused: boolean) {
    setSubmitting(true);
    const endpoint = currentPaused ? "/api/v1/risk/resume" : "/api/v1/risk/pause";
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      if (!response.ok) throw new Error("Failed to change pause state");
      const updated = (await response.json()) as RiskStatus;
      setPauseState({ state: "ready", status: updated });
    } catch {
      alert("Unable to toggle execution pause state. Check local connection.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="panel" aria-labelledby="risk-heading">
      <div className="section-heading">
        <h2 id="risk-heading">Risk &amp; Emergency Controls</h2>
        <button
          type="button"
          disabled={pauseState.state === "loading" || submitting}
          onClick={() => {
            setPauseState({ state: "loading" });
            setRefresh((r) => r + 1);
          }}
        >
          Refresh
        </button>
      </div>

      {pauseState.state === "loading" && <p role="status">Checking risk status…</p>}
      {pauseState.state === "unavailable" && (
        <p role="status">Risk engine status unavailable.</p>
      )}

      {pauseState.state === "ready" && (
        <div className="risk-content">
          {pauseState.status.paused ? (
            <div role="alert" className="alert-banner alert-danger">
              <strong>🚨 EMERGENCY KILL SWITCH ACTIVE:</strong> Autonomous execution
              and order submissions are halted across all venues and on-chain routers.
            </div>
          ) : (
            <div className="risk-ok">
              <span className="available">✓ Pre-trade risk policy active:</span>
              <span className="muted">
                {" "}Strict spot-only envelope ($10–$1,000 notional, max 0.5% spread,
                max 1.0% slippage, persistent kill-switch protection).
              </span>
            </div>
          )}

          <div className="risk-actions">
            <button
              type="button"
              className={
                pauseState.status.paused
                  ? "btn-action btn-resume"
                  : "btn-action btn-danger"
              }
              disabled={submitting}
              onClick={() => handleTogglePause(pauseState.status.paused)}
            >
              {submitting
                ? "Updating…"
                : pauseState.status.paused
                ? "Resume Execution"
                : "Trigger Emergency Kill Switch"}
            </button>
            {pauseState.status.changed_at && (
              <span className="muted risk-timestamp">
                Last changed:{" "}
                <time dateTime={pauseState.status.changed_at}>
                  {timeFormat.format(new Date(pauseState.status.changed_at))}
                </time>
              </span>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
