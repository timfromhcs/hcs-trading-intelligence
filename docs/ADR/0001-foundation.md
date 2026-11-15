# ADR 0001 — Foundation

Status: accepted for Phase 0.

Use Apache-2.0 as specified by the repository's existing LICENSE. The package has no runtime dependencies. CI inventories the licenses of installed development dependencies separately.

Use the uv build backend pinned to the same release as uv itself. The pure-Python package builds without a compiler and keeps the build toolchain small. The lockfile and CI matrix provide the baseline for adding runtime dependencies.
