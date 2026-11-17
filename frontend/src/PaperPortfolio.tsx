import { useEffect, useState } from "react";

interface PaperFill {
  order: { idempotency_key: string; side: "buy" | "sell"; quantity: string };
  price: string;
  fee: string;
  executed_at: string;
}

interface Account {
  mode: "PAPER";
  balance: {
    cash: string;
    quantity: string;
    cost_basis: string;
    fees: string;
    realized_pnl: string;
  };
  fills: PaperFill[];
}

interface Pause {
  paused: boolean;
  changed_at: string | null;
}

type Portfolio =
  | { state: "loading" | "unavailable" }
  | { state: "ready"; account: Account; pause: Pause | null };

const timeFormat = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

async function readLocal<T>(path: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(path, { signal, cache: "no-store" });
  if (!response.ok) throw new Error("Paper account unavailable");
  return (await response.json()) as T;
}

export function PaperPortfolio() {
  const [portfolio, setPortfolio] = useState<Portfolio>({ state: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(5000)]);
    const account = readLocal<Account>("/api/v1/paper/account", signal);
    const pause = readLocal<Pause>("/api/v1/risk/status", signal).catch(() => null);
    Promise.all([account, pause]).then(
      ([account, pause]) => {
        if (!controller.signal.aborted) setPortfolio({ state: "ready", account, pause });
      },
      () => {
        if (!controller.signal.aborted) setPortfolio({ state: "unavailable" });
      },
    );
    return () => controller.abort();
  }, []);

  return (
    <section className="panel" aria-labelledby="paper-heading">
      <h2 id="paper-heading">Simulated portfolio</h2>
      {portfolio.state === "loading" && <p role="status">Loading paper ledger…</p>}
      {portfolio.state === "unavailable" && <p role="status">Paper ledger unavailable.</p>}
      {portfolio.state === "ready" && (
        <>
          <p className={portfolio.pause?.paused ? "quote-warning" : "muted"}>
            {portfolio.pause === null
              ? "Execution pause status unavailable."
              : portfolio.pause.paused ? "Execution paused." : "Execution pause is clear."}
            {portfolio.pause?.changed_at && (
              <>{" · "}<time dateTime={portfolio.pause.changed_at}>
                {timeFormat.format(new Date(portfolio.pause.changed_at))}
              </time></>
            )}
          </p>
          <dl className="status-grid paper-balances">
            <div><dt>Paper cash</dt><dd>{portfolio.account.balance.cash} USD</dd></div>
            <div><dt>Paper position</dt><dd>{portfolio.account.balance.quantity} BTC</dd></div>
            <div><dt>Position cost basis</dt><dd>{portfolio.account.balance.cost_basis} USD</dd></div>
            <div><dt>Simulated fees</dt><dd>{portfolio.account.balance.fees} USD</dd></div>
            <div><dt>Realized P&amp;L</dt><dd>{portfolio.account.balance.realized_pnl} USD</dd></div>
          </dl>
          <ul className="paper-notes muted" aria-label="Accounting notes">
            <li>Accounting value is simulated and denominated in USD (reporting currency). It is not a wallet balance.</li>
            <li>Unrealized P&amp;L: unavailable, because the account endpoint carries no mark price.</li>
            <li>Hot/cold split: this paper account holds no custody. A cold reserve is never tracked by this application.</li>
            <li>Fiat USD and stablecoins are distinct assets and are not combined here.</li>
          </ul>
          <h3 className="paper-fills-heading">Recent simulated fills</h3>
          {portfolio.account.fills.length === 0 ? <p className="muted">No fills recorded.</p> : (
            <ul className="paper-fills">
              {portfolio.account.fills.map((fill) => (
                <li key={fill.order.idempotency_key}>
                  <strong>{fill.order.side === "buy" ? "Buy" : "Sell"} {fill.order.quantity} BTC</strong>
                  <span>{fill.price} USD/BTC · {fill.fee} USD fee</span>
                  <time dateTime={fill.executed_at}>{timeFormat.format(new Date(fill.executed_at))}</time>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
