# Security policy

HCS Trading Intelligence is pre-release software. It is not audited and has no supported production version.

## Supported versions

| Version | Status |
|---|---|
| 0.2.x (pre-release) | Security fixes on `main` and the latest 0.2.x tag only |
| < 0.2 | Not supported |

Fixes ship in a new pre-release. Older tags are not patched.

## Reporting a vulnerability

Report vulnerabilities privately through GitHub's **Report a vulnerability** feature on the repository's Security tab when it is enabled. Otherwise contact the maintainer privately through the contact details on the GitHub profile of `timfromhcs`. Do not open a public issue, pull request or discussion for a suspected vulnerability.

Include the affected version or commit, the impact, reproduction conditions and a minimal redacted example. Keep credentials, seed phrases, private keys, keystore passwords, API keys, wallet material and identifiable transaction data out of reports.

## Scope

In scope:

- The risk engine, kill switch, signer, keystore and transaction decoders and validators.
- Wallet, exchange and swap adapters, including network, chain and genesis identity checks.
- The local REST API and MCP server, including bind address, authorization and capability tiers.
- Prompt-injection paths from news, web content, model output or tool results into actions or configuration.
- Secret handling in logs, storage, build artifacts and crash output.
- The build, packaging and release pipeline in `.github/` and `scripts/`.

Out of scope:

- Trading losses, strategy performance or market outcomes. These are not vulnerabilities.
- Vulnerabilities in third-party services, exchanges, RPC providers, nodes or model runtimes. Report these upstream. Tell us if this project uses them unsafely.
- Attacks that require an already compromised operating system account or administrator access, unless the application makes the compromise materially worse.
- Configurations that expose the API or MCP server beyond loopback against the documented defaults.
- Findings from automated scanners without a demonstrated impact.

## Testing rules

Never test with mainnet funds, real wallets, real exchange accounts or other people's systems. Use fixtures, regtest, devnet or testnet, synthetic keys and an isolated data directory. Do not run tests that place real orders or broadcast mainnet transactions.

## Response targets

These are targets for a volunteer-maintained pre-release project, not guarantees.

- Acknowledgement within 3 business days.
- Initial triage and severity assessment within 10 business days.
- Status updates at least every 14 days while a report is open.
- A fix or mitigation for a confirmed critical or high issue in the signer, wallet, risk, MCP or release paths before further releases.

## Disclosure

Maintainers triage reports privately, prioritize containment and regression tests, and coordinate disclosure with the reporter after a fix or mitigation is available. The default embargo is 90 days from the report, or less once a fix ships. Reporters are credited in the advisory unless they ask otherwise.
