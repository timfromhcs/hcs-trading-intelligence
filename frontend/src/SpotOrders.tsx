import { useEffect, useState } from "react";

interface SpotOrder {
  client_order_id: string;
  order_id: string;
  venue: string;
  market: string;
  side: "buy" | "sell";
  order_type: "limit" | "market";
  quantity: string;
  limit_price: string | null;
  state:
    | "PENDING_VALIDATION"
    | "VALIDATED"
    | "SUBMITTED"
    | "ACKNOWLEDGED"
    | "PARTIALLY_FILLED"
    | "FILLED"
    | "CANCELED"
    | "EXPIRED"
    | "REJECTED"
    | "RECONCILIATION_REQUIRED";
  risk_decision_id: string;
  created_at: string;
  updated_at: string;
  venue_order_id: string | null;
  filled_quantity: string | null;
  average_price: string | null;
  fee: string | null;
}

type OrderSnapshot =
  | { state: "loading" }
  | { state: "unavailable" }
  | { state: "ready"; orders: SpotOrder[] };

const timeFormat = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

async function readSpotOrders(signal: AbortSignal): Promise<SpotOrder[]> {
  const response = await fetch("/api/v1/orders/spot?venue=kraken", {
    signal,
    cache: "no-store",
  });
  if (!response.ok) throw new Error("Spot orders unavailable");
  return (await response.json()) as SpotOrder[];
}

export function SpotOrders() {
  const [snapshot, setSnapshot] = useState<OrderSnapshot>({ state: "loading" });
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(5000)]);
    readSpotOrders(signal).then(
      (orders) => {
        if (!controller.signal.aborted) setSnapshot({ state: "ready", orders });
      },
      () => {
        if (!controller.signal.aborted) setSnapshot({ state: "unavailable" });
      },
    );
    return () => controller.abort();
  }, [refresh]);

  const hasReconciliation =
    snapshot.state === "ready" &&
    snapshot.orders.some((o) => o.state === "RECONCILIATION_REQUIRED");

  return (
    <section className="panel" aria-labelledby="spot-orders-heading">
      <div className="section-heading">
        <h2 id="spot-orders-heading">Spot Execution Orders (Kraken)</h2>
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

      {hasReconciliation && (
        <div role="alert" className="alert-banner alert-danger">
          <strong>Reconciliation required:</strong> One or more spot orders have
          ambiguous state and require manual venue verification before trading can resume.
        </div>
      )}

      {snapshot.state === "loading" && <p role="status">Loading spot orders…</p>}
      {snapshot.state === "unavailable" && (
        <p role="status">Spot orders unavailable. Check local service.</p>
      )}

      {snapshot.state === "ready" && (
        <>
          {snapshot.orders.length === 0 ? (
            <p className="muted">No open or recent spot orders recorded.</p>
          ) : (
            <ul className="order-list">
              {snapshot.orders.map((order) => (
                <li key={order.client_order_id} className="order-item">
                  <div className="order-main">
                    <span className={`order-badge badge-${order.side}`}>
                      {order.side.toUpperCase()}
                    </span>
                    <strong>
                      {order.quantity} {order.market}
                    </strong>
                    {order.limit_price && (
                      <span className="order-price">@ {order.limit_price}</span>
                    )}
                    <span className="order-state" data-state={order.state}>
                      {order.state}
                    </span>
                  </div>
                  <div className="order-meta">
                    <span>Client ID: <code>{order.client_order_id}</code></span>
                    {order.venue_order_id && (
                      <span>Venue ID: <code>{order.venue_order_id}</code></span>
                    )}
                    <span>Risk Ref: <code>{order.risk_decision_id}</code></span>
                    <time dateTime={order.updated_at}>
                      {timeFormat.format(new Date(order.updated_at))}
                    </time>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
