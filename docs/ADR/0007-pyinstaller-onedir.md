# ADR 0007 — PyInstaller one-folder desktop builds

Status: accepted for development builds.

## Context

The product needs a runnable desktop artifact without a Python install. PyInstaller cannot cross-compile, and one-file mode unpacks into a temporary directory, which is slower and more exposed to antivirus handling.

## Decision

Build and smoke-test the one-folder layout on a native runner for each operating system. Keep user data, the database and any model files in the operating system data directory, never in the extraction path. A Windows single EXE is built only after the one-folder build passes. Archives get a SHA-256 checksum.

## Consequences

- Artifacts are unsigned development builds. Signing, notarization, SBOM and provenance remain release work.
- Nuitka stays an option to benchmark later.
