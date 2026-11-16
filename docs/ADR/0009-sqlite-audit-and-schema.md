# ADR 0009 — SQLite state, audit events and migrations

Status: accepted.

## Context

State must survive crashes and restarts, support audit, and need no server.

## Decision

Use SQLite with WAL, foreign keys, explicit transactions and `PRAGMA user_version` migrations that refuse a database newer than the code. The schema version is defined by `_SCHEMA_VERSION` in `storage.py`, and documents refer to that constant rather than copy the number. Audit events are append-only from the application view. Backups use SQLite online backup. Secrets are never stored. Wallets from other networks use separate database files so a test network cannot mix with mainnet data.

## Consequences

- A downgrade is refused. Back up before upgrading.
- Local administrators can still edit files, so the audit trail is evidence, not proof.
