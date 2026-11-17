import { useEffect, useState } from "react";

const statusLabels = {
  available: "Assessment available",
  model_disabled: "Model disabled",
  unavailable: "Model unavailable",
  insufficient_data: "RSI warming up",
};

type RunStatus = keyof typeof statusLabels;

interface ResearchHistory {
  runs: {
    run_id: string;
    started_at: string;
    duration_ms: number;
    interval_minutes: number;
    status: RunStatus;
    model: string | null;
    evidence: { rsi14: string | null; closed_at: string | null };
  }[];
  statistics: {
    status: RunStatus;
    count: number;
    mean_duration_ms: number;
  }[];
}

type Snapshot = ResearchHistory | "loading" | "unavailable";

const dateTime = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});
const duration = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
const rsi = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });

export function ResearchRuns() {
  const [snapshot, setSnapshot] = useState<Snapshot>("loading");
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(5000)]);
    fetch("/api/v1/research/runs?limit=25", { signal, cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) throw new Error("Research history unavailable");
        return (await response.json()) as ResearchHistory;
      })
      .then(
        (history) => {
          if (!controller.signal.aborted) setSnapshot(history);
        },
        () => {
          if (!controller.signal.aborted) setSnapshot("unavailable");
        },
      );
    return () => controller.abort();
  }, [refresh]);

  return (
    <section className="panel" aria-labelledby="research-runs-heading">
      <div className="section-heading">
        <h2 id="research-runs-heading">Local Research Runs</h2>
        <button
          type="button"
          disabled={snapshot === "loading"}
          onClick={() => {
            setSnapshot("loading");
            setRefresh((count) => count + 1);
          }}
        >
          Refresh
        </button>
      </div>
      {snapshot === "loading" && <p role="status">Loading research history…</p>}
      {snapshot === "unavailable" && (
        <p role="alert">Research history is unavailable. Try refreshing.</p>
      )}
      {typeof snapshot === "object" && snapshot.runs.length === 0 && (
        <p role="status">
          No research runs yet. Start one with <code>hcs-trader research-brief</code>.
        </p>
      )}
      {typeof snapshot === "object" && snapshot.runs.length > 0 && (
        <>
          <h3>All-time statistics</h3>
          <dl className="status-grid">
            {snapshot.statistics.map((stat) => (
              <div key={stat.status}>
                <dt>{statusLabels[stat.status]}</dt>
                <dd>{stat.count} · {duration.format(stat.mean_duration_ms)} ms avg</dd>
              </div>
            ))}
          </dl>
          <h3 id="recent-research-heading">Recent runs</h3>
          <div className="research-runs-scroll" tabIndex={0} role="region" aria-labelledby="recent-research-heading">
            <table className="data-table" aria-labelledby="recent-research-heading">
              <thead>
                <tr>
                  <th scope="col">Started</th>
                  <th scope="col">Result</th>
                  <th scope="col">Model</th>
                  <th scope="col">Duration</th>
                  <th scope="col">RSI (14)</th>
                  <th scope="col">Last candle close</th>
                </tr>
              </thead>
              <tbody>
                {snapshot.runs.map((run) => (
                  <tr key={run.run_id}>
                    <td><time dateTime={run.started_at}>{dateTime.format(new Date(run.started_at))}</time></td>
                    <td>{statusLabels[run.status]}</td>
                    <td>{run.model ?? "—"}</td>
                    <td>{duration.format(run.duration_ms)} ms</td>
                    <td>{run.evidence.rsi14 === null ? "—" : rsi.format(Number(run.evidence.rsi14))}</td>
                    <td>
                      {run.evidence.closed_at === null ? "—" : (
                        <time dateTime={run.evidence.closed_at}>
                          {dateTime.format(new Date(run.evidence.closed_at))}
                        </time>
                      )}
                      {" · "}{run.interval_minutes} min
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}
