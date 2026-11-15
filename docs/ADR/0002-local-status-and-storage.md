# ADR 0002 — Local status and storage

Status: accepted for the read-only foundation.

The API and built React shell share one origin at `127.0.0.1:8765`. There are only read endpoints. CLI and API status are generated from the same schema. Browser dependencies are bundled in the wheel and the service can start without contacting a CDN.

Use Python's SQLite driver for the initial audit store. The single versioned schema change runs under an immediate transaction. Connections enable foreign keys, and the database uses WAL mode. The storage module owns SQL and provides an online snapshot for backup/restore testing. Encrypted operator backups and migrations for wallet and trading state are future work.
