# Threat model draft

The current trust boundaries are the local CLI, source and lockfiles, the dependency registries, and CI runners. Future boundaries include provider responses, REST and MCP clients, local persistent state, the execution adapter and the signer. The CLI accepts arguments and emits only build metadata and static capabilities.

- **Spoofing** — lookalike packages and fake market or chain identities. Lock package sources and hashes now. Validate provider network and canonical asset identity when adapters are added.
- **Tampering** — compromised workflow dependencies, altered orders or local database edits. Pin CI actions by commit. Later bind approval and simulation to the same transaction hash.
- **Repudiation** — a client denies proposing a trade. Later record correlation IDs and immutable application events. Local administrator access can alter files.
- **Information disclosure** — credentials leak into source, CI, logs or model context. Keep fixtures synthetic, CI free of trading secrets, and future keys inside the signer boundary.
- **Denial of service** — provider outage, exhausted rate limit, database or disk failure. Later use bounded retries, persisted state and fail-closed execution.
- **Elevation of privilege** — untrusted model output or client requests bypass policy. The current runtime exposes neither private state nor execution. Future transports must share deterministic authorization, with a separate signer check.

A compromised local administrator or CI runner remains outside the protection of application-level controls. Review this draft when introducing persistence, network transports, wallets or execution.
