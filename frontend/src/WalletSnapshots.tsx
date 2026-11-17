import { useEffect, useState } from "react";

interface SnapshotItem {
  address: string;
  asset: string;
  base_units: string;
  decimals: number;
  observed_at: string;
  block_number?: number;
  block_hash?: string;
  slot?: number;
  genesis_hash?: string;
}

type WalletState =
  | { state: "loading" }
  | { state: "unavailable" }
  | { state: "ready"; snapshots: SnapshotItem[] };

const timeFormat = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

function formatUnits(baseUnits: string, decimals: number): string {
  if (decimals === 0) return baseUnits;
  const raw = baseUnits.padStart(decimals + 1, "0");
  const intPart = raw.slice(0, raw.length - decimals) || "0";
  const fracPart = raw.slice(raw.length - decimals).replace(/0+$/, "");
  return fracPart ? `${intPart}.${fracPart}` : intPart;
}

function shortenAddress(addr: string): string {
  if (addr.length <= 16) return addr;
  return `${addr.slice(0, 8)}…${addr.slice(-6)}`;
}

async function readWallets(signal: AbortSignal): Promise<SnapshotItem[]> {
  const response = await fetch("/api/v1/wallets?limit=25", {
    signal,
    cache: "no-store",
  });
  if (!response.ok) throw new Error("Wallet snapshots unavailable");
  return (await response.json()) as SnapshotItem[];
}

export function WalletSnapshots() {
  const [walletState, setWalletState] = useState<WalletState>({ state: "loading" });
  const [refresh, setRefresh] = useState(0);
  const [reveal, setReveal] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(5000)]);
    readWallets(signal).then(
      (snapshots) => {
        if (!controller.signal.aborted) setWalletState({ state: "ready", snapshots });
      },
      () => {
        if (!controller.signal.aborted) setWalletState({ state: "unavailable" });
      },
    );
    return () => controller.abort();
  }, [refresh]);

  return (
    <section className="panel" aria-labelledby="custody-heading">
      <div className="section-heading">
        <h2 id="custody-heading">On-Chain Custody &amp; Wallets</h2>
        <button
          type="button"
          disabled={walletState.state === "loading"}
          onClick={() => {
            setWalletState({ state: "loading" });
            setRefresh((r) => r + 1);
          }}
        >
          Refresh
        </button>
      </div>

      <div className="custody-notice">
        <strong>Cold Reserve Isolation Boundary:</strong> Long-term cold reserves
        must remain in separate cold accounts whose private keys are never loaded into
        the running software. The hot trading balance is strictly bounded by
        deterministic risk policy.
      </div>

      <p className="muted">
        Observed balances are watch-only wallet balances, not accounting value. Hot/cold
        classification and hardware status are not provided by the service; treat every
        address here as hot.
      </p>
      <button type="button" aria-pressed={reveal} onClick={() => setReveal((r) => !r)}>
        {reveal ? "Hide addresses" : "Reveal addresses"}
      </button>
      <details className="chart-table">
        <summary>Recovery and security checklist</summary>
        <ul>
          <li>Keep the cold reserve in a separate wallet whose key is never loaded here.</li>
          <li>Back up seed material offline; never paste it into this application.</li>
          <li>Fund the hot wallet only with an amount you can afford to lose.</li>
          <li>Verify the network and address before any transfer.</li>
        </ul>
      </details>

      {walletState.state === "loading" && <p role="status">Loading wallet state…</p>}
      {walletState.state === "unavailable" && (
        <p role="status">Wallet snapshots unavailable.</p>
      )}

      {walletState.state === "ready" && (
        <>
          {walletState.snapshots.length === 0 ? (
            <p className="muted">No on-chain wallet snapshots recorded yet.</p>
          ) : (
            <div className="status-grid wallet-grid">
              {walletState.snapshots.map((item) => (
                <div key={`${item.address}-${item.asset}`} className="wallet-card">
                  <dt>
                    <span className="wallet-asset">{item.asset}</span>
                    <span className="wallet-address" title={reveal ? item.address : undefined}>
                      {reveal ? shortenAddress(item.address) : "Address hidden"}
                    </span>
                  </dt>
                  <dd>
                    <span className="wallet-balance">
                      {formatUnits(item.base_units, item.decimals)} {item.asset}
                    </span>
                    <span className="wallet-meta">
                      {item.block_number !== undefined && (
                        <span>Block #{item.block_number}</span>
                      )}
                      {item.slot !== undefined && <span>Slot #{item.slot}</span>}
                      <time dateTime={item.observed_at}>
                        {timeFormat.format(new Date(item.observed_at))}
                      </time>
                    </span>
                  </dd>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </section>
  );
}
