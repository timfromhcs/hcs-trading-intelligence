# Known limitations register

AGENTS.md section 13.3: any untested security-critical branch must be listed here, and
every `security-critical` entry whose status is `untested` blocks LIVE enablement
(`hcs-trader live-enable`) and the `R-05` release-check item. Change an entry's status only
with linked evidence in the same commit.

Line format: `- KL-nnn [security-critical|other] [untested|tested|accepted] text`

- KL-001 [security-critical] [untested] No authenticated venue evidence: the Binance Spot Testnet adapter, rehearsal flow and Kraken validate probe exist and pass fixture tests, but have not been run against any live endpoint (needs operator-created testnet keys); Kraken matching, fills and ambiguous outcomes remain fixture-only.
- KL-002 [security-critical] [untested] Chain-valid signing exists offline for Sepolia EVM (allowlisted SwapRouter02 and exact approve), Solana messages (solders) and a regtest-only node-signed PSBT flow, all covered by offline tests; no testnet/devnet broadcast acceptance and no independent security review yet, so on-chain LIVE routes stay disabled.
- KL-003 [security-critical] [untested] Clean-machine install, upgrade and kill-switch-through-crash behaviour have not been evidenced on all three operating systems.
- KL-004 [other] [accepted] Direct BTC, LTC and DGB on-chain spending is not supported; only read-only wallet adapters exist.
- KL-005 [other] [accepted] TODO raise to 90 line coverage (AGENTS.md 13.3 target): signing.py (75), spot_execution.py (87), domain/signing.py (86) and evm_signing.py (85) are below 90; solana_signing.py now meets 90. Per-module floors in scripts/check_coverage_floors.py stop further decline. Untested security-critical branches inside them must still be listed as security-critical entries.
- KL-006 [security-critical] [untested] SafeFetch validates every resolved address and re-resolves after retrieval, but the HTTP client does not pin the TCP connection to the validated IP; an attacker able to control DNS between resolution and socket connect could still redirect the request. No live source is enabled, so no production request path is exposed. Socket-level pinning needs a transport integration test before any source is enabled.
- KL-007 [other] [accepted] All sources in `data/sources.json` ship with `enabled=false`, `result=pending` and `robots=pending`; Wave 2 verification records and recorded fixtures are still missing. Wave 2 covers only Coinbase public REST; Binance data-api and a real WebSocket transport remain unimplemented. News/macro ingestion therefore continues to use the already verified providers only.
- KL-008 [other] [accepted] The optional local-model catalog holds advisory candidates only; no entries have operator-supplied manifests or verified checksums in this repository. OpenJEV and System One JEV are unverified aliases, not published models. The eval harness uses deterministic offline fixtures, not a live model.
