/** Shared helpers for talking to the local REST API (UI boundary only). */

export function requestSignal(controller: AbortController, timeoutMs = 5000): AbortSignal {
  const timeout = AbortSignal.timeout(timeoutMs);
  if (typeof AbortSignal.any === "function") {
    return AbortSignal.any([controller.signal, timeout]);
  }
  return controller.signal;
}

export async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { signal, cache: "no-store" });
  if (!response.ok) throw new Error(`Request failed: ${response.status}`);
  return (await response.json()) as T;
}

export async function postJson<T>(path: string): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  if (!response.ok) throw new Error(`Request failed: ${response.status}`);
  return (await response.json()) as T;
}

export type Mode = "OFFLINE" | "DATA_ONLY" | "PAPER" | "LIVE";

export interface SystemStatus {
  mode: Mode;
  execution_available: boolean;
  network_enabled: boolean;
}

export interface Capabilities {
  api: boolean;
  market_data: boolean;
  portfolio: boolean;
  paper_orders: boolean;
  live_orders: boolean;
  mcp: boolean;
  wallet_snapshots?: boolean;
}

export interface RiskStatus {
  paused: boolean;
  changed_at: string | null;
}
