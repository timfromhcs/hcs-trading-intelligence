import { useState } from "react";
import { isPositive, multiplyDecimals } from "./decimal";

export const LIVE_CONFIRMATION = "ENABLE LIVE TRADING";

/** Local, validation-only order preview. Nothing is sent to any service. */
export function OrderPreview({ liveAvailable }: { liveAvailable: boolean }) {
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [quantity, setQuantity] = useState("");
  const [price, setPrice] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [typed, setTyped] = useState("");
  const [activationNote, setActivationNote] = useState(false);

  const valid = isPositive(quantity) && isPositive(price);
  const notional = valid ? multiplyDecimals(quantity, price) : null;

  return (
    <section className="panel" aria-labelledby="preview-heading">
      <h2 id="preview-heading">Order preview (validation only)</h2>
      <p className="muted">
        This form computes a local preview. It never submits an order. Fee, slippage, route
        details, minimum received and risk budget come from the service risk gate, and no
        preview endpoint exists yet.
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setSubmitted(true);
        }}
      >
        <div className="form-row">
          <label>
            Side
            <select value={side} onChange={(e) => setSide(e.target.value as "buy" | "sell")}>
              <option value="buy">Buy</option>
              <option value="sell">Sell</option>
            </select>
          </label>
          <label>
            Asset
            <input value="BTC/USD (spot)" readOnly />
          </label>
          <label>
            Amount (BTC)
            <input
              inputMode="decimal"
              value={quantity}
              onChange={(e) => {
                setQuantity(e.target.value);
                setSubmitted(false);
              }}
            />
          </label>
          <label>
            Limit price (USD)
            <input
              inputMode="decimal"
              value={price}
              onChange={(e) => {
                setPrice(e.target.value);
                setSubmitted(false);
              }}
            />
          </label>
          <button type="submit">Preview</button>
        </div>
      </form>
      {submitted && !valid && (
        <p role="alert" className="quote-warning">
          Amount and price must be positive decimal numbers.
        </p>
      )}
      {submitted && valid && (
        <dl className="status-grid preview-result" aria-label="Order preview">
          <div><dt>Side</dt><dd>{side === "buy" ? "Buy" : "Sell"}</dd></div>
          <div><dt>Asset</dt><dd>BTC/USD spot</dd></div>
          <div><dt>Amount</dt><dd>{quantity} BTC</dd></div>
          <div><dt>Estimated price</dt><dd>{price} USD</dd></div>
          <div><dt>Notional</dt><dd>{notional} USD</dd></div>
          <div><dt>Fee</dt><dd>Not provided by service</dd></div>
          <div><dt>Slippage</dt><dd>Not provided by service</dd></div>
          <div><dt>Route / destination</dt><dd>Kraken spot venue (no on-chain destination)</dd></div>
          <div><dt>Minimum received</dt><dd>Not provided by service</dd></div>
          <div><dt>Risk budget</dt><dd>Not provided by service</dd></div>
        </dl>
      )}

      <h3 className="paper-fills-heading">Live trading activation</h3>
      <p className="muted">
        LIVE mode is configured locally with hard limits before the application starts. This
        screen cannot enable it.
      </p>
      <label>
        Type <code>{LIVE_CONFIRMATION}</code> to acknowledge the risk of loss
        <input value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
      </label>
      <button
        type="button"
        className="btn-action btn-danger"
        disabled={!liveAvailable || typed !== LIVE_CONFIRMATION}
        onClick={() => setActivationNote(true)}
      >
        Enable live trading
      </button>
      {activationNote && (
        <p role="status">
          Acknowledged. Live trading can only be enabled through local configuration; no
          change was made.
        </p>
      )}
      {!liveAvailable && (
        <p className="muted">Disabled: no live route is configured on this installation.</p>
      )}
    </section>
  );
}
