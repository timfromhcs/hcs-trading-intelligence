import { useEffect, useState } from "react";
import { getJson, requestSignal } from "./api";

type Quality = "FRESH" | "DEGRADED" | "STALE" | "MISSING" | "INVALID";

export interface SourceStatus {
  source_id: string;
  kind: string;
  quality: Quality;
  last_success: string | null;
  last_error: string | null;
  consecutive_failures: number;
  enabled: boolean;
  requires_api_key: boolean;
  breaker_state: "CLOSED" | "OPEN" | "HALF_OPEN";
  retry_in_seconds: number | null;
}

interface DataSourcesBody {
  ingestion_enabled: boolean;
  ingestion_mode: string | null;
  sources: SourceStatus[];
}

const labels: Record<Quality, string> = {
  FRESH: "Fresh",
  DEGRADED: "Degraded",
  STALE: "Stale",
  MISSING: "No data yet",
  INVALID: "Invalid",
};

function describe(source: SourceStatus): string {
  if (!source.enabled) {
    return source.requires_api_key ? "Disabled (API key required)" : "Disabled";
  }
  return labels[source.quality];
}

export function DataSources() {
  const [data, setData] = useState<DataSourcesBody | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    let retry: ReturnType<typeof setTimeout> | undefined;

    async function refresh() {
      try {
        const next = await getJson<DataSourcesBody>(
          "/api/v1/data/sources",
          requestSignal(controller, 8000),
        );
        if (!controller.signal.aborted) {
          setData(next);
          setError(false);
        }
      } catch {
        if (!controller.signal.aborted) setError(true);
      } finally {
        if (!controller.signal.aborted) retry = setTimeout(refresh, 15_000);
      }
    }

    void refresh();
    return () => {
      controller.abort();
      clearTimeout(retry);
    };
  }, []);

  return (
    <section className="panel" aria-labelledby="sources-heading">
      <h2 id="sources-heading">Data sources</h2>
      <div role="status">
        {error && <p className="quote-warning">Source status unavailable.</p>}
        {!error && data === null && <p>Loading source status…</p>}
        {!error && data !== null && (
          <p className="muted">
            {data.ingestion_enabled
              ? `Background ingestion is on (${data.ingestion_mode}).`
              : "Background ingestion is off; sources update only when imported."}
          </p>
        )}
      </div>
      {!error && data !== null && (
        <table>
          <thead>
            <tr>
              <th scope="col">Source</th>
              <th scope="col">Freshness</th>
              <th scope="col">Breaker</th>
              <th scope="col">Retry in</th>
              <th scope="col">Last success</th>
            </tr>
          </thead>
          <tbody>
            {data.sources.map((s) => (
              <tr key={s.source_id}>
                <th scope="row">{s.source_id}</th>
                <td className={s.quality === "FRESH" ? "available" : "quote-warning"}>
                  {describe(s)}
                  {s.last_error && s.enabled && <> · {s.last_error}</>}
                </td>
                <td>{s.breaker_state}</td>
                <td>{s.retry_in_seconds === null ? "-" : `${Math.ceil(s.retry_in_seconds)} s`}</td>
                <td>
                  {s.last_success === null ? (
                    "-"
                  ) : (
                    <time dateTime={s.last_success}>{new Date(s.last_success).toLocaleString()}</time>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
