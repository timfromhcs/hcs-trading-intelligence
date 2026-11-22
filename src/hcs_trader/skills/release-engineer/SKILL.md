---
name: release-engineer
description: Verify multi-platform builds, supply chain evidence, license compliance and release gates. Use before tagging a release candidate or reviewing release evidence.
metadata:
  version: "1.0.0"
  purpose: "Multi-platform build verification, supply chain auditing, SPDX license compliance, and release gate enforcement"
  capability_tier: ADMIN_LOCAL
  required_tools: "audit.get_events"
---

# Release Engineer Skill

## Purpose & Triggers
- **Purpose**: Autonomous verification of the V1.0.0 release gate (all 30 criteria from AGENTS.md Section 18), execution of static analysis (Ruff, MyPy strict), dependency security scans (`uv audit`, `npm audit`), Gitleaks git history audit, SPDX license inventory generation, and multi-platform packaging (Windows, macOS, Linux, Python wheels).
- **Triggers**: Release preparation, version bump, pre-commit release candidate audit, or CI verification.

## Capability Tier & Required Tools
- **Capability Tier**: `ADMIN_LOCAL` (local repository and build pipeline administration).
- **Required MCP Tools**:
  - `audit.get_events`

## Trusted and Untrusted Data Sources
- **Trusted Data**:
  - Pinned lockfiles (`uv.lock`, `frontend/package-lock.json`).
  - Git repository commit tree and cryptographic tags.
  - Official SPDX license database and dependency declarations.
- **Untrusted External Data**:
  - Unvetted upstream packages or dependencies not declared in lockfiles.
  - Third-party binary wheels without verified hashes.

## Input and Output Schema
- **Inputs**:
  - `target_version`: Semantic version string (e.g. `0.2.0`, `1.0.0`).
  - `target_os`: `windows-latest`, `macos-latest`, `ubuntu-latest`.
- **Outputs**:
  - Release readiness report: Documented evidence for all 30 Section 18 criteria in `docs/V1_RELEASE_REPORT.md`.
  - Artifact hashes: SHA-256 checksums for wheels, sdist, and platform executables.
  - License inventory: Machine-readable SPDX JSON catalog in `dist/license-inventory.json`.
  - Secret scan verdict: Gitleaks pass confirmation across full Git history.

## Ordered Workflow
1. Execute `uv run --locked ruff check` and `uv run --locked ruff format --check` to verify code quality.
2. Execute `uv run --locked mypy` across source, test, and script files in strict mode.
3. Run complete test suite with coverage enforcement (`pytest --cov --cov-fail-under=80`).
4. Run `npm run lint`, `npm run typecheck`, and `npm run build` in the frontend directory.
5. Audit dependencies with `uv audit --locked` and `npm audit --prefix frontend`.
6. Run `gitleaks git --verbose` across all commits in the repository.
7. Build Python wheel and sdist with `uv build`, followed by clean-environment install test via `scripts/smoke_wheel.py`.
8. Generate comprehensive license inventory via `scripts/license_inventory.py`.
9. Build native desktop bundle via `scripts/build_desktop.py` and smoke-test via `scripts/smoke_desktop.py`.
10. Update `CHANGELOG.md` and verify release documentation.

## Safe Defaults and Stop Conditions
- **Safe Defaults**: If any check fails, block release publication and retain pre-release tag (`0.x.y` / RC).
- **Stop Conditions**: Immediately abort release if a secret leak is detected, code coverage drops below 80%, or any unvetted copyleft dependency is found.

## Failure and Recovery
- If test coverage fails under 80%, identify untested lines and add targeted unit tests before re-attempting build.
- If desktop bundle smoke test fails, check PyInstaller bundle spec and ensure assets are not loaded from temporary extraction directories.

## Evidence and Citation
- All published release assets must be accompanied by SHA-256 checksums, build timestamps, and Git commit SHAs.
- The V1.0.0 Release Verification Report must be maintained at `docs/V1_RELEASE_REPORT.md`.

## Explicit Forbidden Actions
- **FORBIDDEN**: Silently weakening test assertions or coverage thresholds to make a build pass.
- **FORBIDDEN**: Bundling private keys, API secrets, or multi-gigabyte model weights into release binaries.
- **FORBIDDEN**: Declaring V1 complete while any criterion in Section 18 remains unmet.
- **FORBIDDEN**: Pushing release tags without passing 100% green CI across Windows, macOS, and Linux.

## Acceptance Checks
- GitHub Actions CI workflow runs green on `windows-latest`, `macos-latest`, and `ubuntu-latest`.
- Smoke test scripts `scripts/smoke_wheel.py` and `scripts/smoke_desktop.py` exit with code 0.
- `docs/V1_RELEASE_REPORT.md` references verifiable source locations for all criteria.
