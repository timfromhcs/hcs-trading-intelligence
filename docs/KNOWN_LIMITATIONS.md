# Known limitations register

AGENTS.md section 13.3: any untested security-critical branch must be listed here, and
every `security-critical` entry whose status is `untested` blocks LIVE enablement
(`hcs-trader live-enable`) and the `R-05` release-check item. Change an entry's status only
with linked evidence in the same commit.

Line format: `- KL-nnn [security-critical|other] [untested|tested|accepted] text`

- KL-001 [security-critical] [untested] No authenticated Kraken venue evidence: order submission, cancellation and ambiguous-outcome recovery have only been exercised against fixtures.
- KL-002 [security-critical] [untested] The isolated signer produces test-envelope signatures only; no chain-valid Ethereum, Bitcoin-family or Solana transaction signing exists, so on-chain LIVE routes stay disabled.
- KL-003 [security-critical] [untested] Clean-machine install, upgrade and kill-switch-through-crash behaviour have not been evidenced on all three operating systems.
- KL-004 [other] [accepted] Direct BTC, LTC and DGB on-chain spending is not supported; only read-only wallet adapters exist.
