import { useEffect, useState } from "react";

interface AuditEvent {
  event_id: string;
  recorded_at: string;
  kind: string;
}

type AuditSnapshot =
  | { state: "loading" }
  | { state: "unavailable" }
  | { state: "ready"; events: AuditEvent[] };

const timeFormat = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

async function readAuditEvents(signal: AbortSignal): Promise<AuditEvent[]> {
  const response = await fetch("/api/v1/audit/events?limit=50", {
    signal,
    cache: "no-store",
  });
  if (!response.ok) throw new Error("Audit log unavailable");
  return (await response.json()) as AuditEvent[];
}

export function AuditLog() {
  const [snapshot, setSnapshot] = useState<AuditSnapshot>({ state: "loading" });
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(5000)]);
    readAuditEvents(signal).then(
      (events) => {
        if (!controller.signal.aborted) setSnapshot({ state: "ready", events });
      },
      () => {
        if (!controller.signal.aborted) setSnapshot({ state: "unavailable" });
      },
    );
    return () => controller.abort();
  }, [refresh]);

  return (
    <section className="panel" aria-labelledby="audit-heading">
      <div className="section-heading">
        <h2 id="audit-heading">Append-Only Audit Log</h2>
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
        {snapshot.state === "loading" && <p>Loading audit events…</p>}
        {snapshot.state === "unavailable" && (
          <p className="error" role="alert">
            Audit event log unavailable. Ensure local service is running.
          </p>
        )}
        {snapshot.state === "ready" && snapshot.events.length === 0 && (
          <p className="muted">No audit events recorded yet.</p>
        )}
        {snapshot.state === "ready" && snapshot.events.length > 0 && (
          <div className="table-wrapper">
            <table className="data-table" aria-label="Audit Events">
              <thead>
                <tr>
                  <th scope="col">Event ID</th>
                  <th scope="col">Recorded At</th>
                  <th scope="col">Event Type</th>
                </tr>
              </thead>
              <tbody>
                {snapshot.events.map((e) => (
                  <tr key={e.event_id}>
                    <td>
                      <code>{e.event_id.slice(0, 12)}…</code>
                    </td>
                    <td>{timeFormat.format(new Date(e.recorded_at))}</td>
                    <td>
                      <span
                        className={`badge badge-${
                          e.kind.includes("paused")
                            ? "danger"
                            : e.kind.includes("resumed")
                              ? "success"
                              : "neutral"
                        }`}
                      >
                        {e.kind}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}
