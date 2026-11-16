import { vi } from "vitest";

/** Route-table fetch fixture. Mocks only the UI/network boundary. */
export function stubFetch(
  routes: Record<string, unknown>,
  calls: { path: string; method: string }[] = [],
) {
  const impl = (input: RequestInfo | URL, init?: RequestInit) => {
    const path = typeof input === "string" ? input : input.toString();
    calls.push({ path, method: init?.method ?? "GET" });
    const key = Object.keys(routes)
      .sort((a, b) => b.length - a.length)
      .find((k) => path.startsWith(k));
    if (key === undefined) return Promise.resolve(new Response("nope", { status: 503 }));
    return Promise.resolve(
      new Response(JSON.stringify(routes[key]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
  };
  vi.stubGlobal("fetch", vi.fn(impl));
  return calls;
}
