# Desktop development builds

Build on the same operating system and architecture as the target. The project uses PyInstaller's one-folder format at this stage. [PyInstaller documentation](https://pyinstaller.org/en/stable/usage.html)

```sh
npm ci --prefix frontend
npm run build --prefix frontend
uv run --locked --group packaging python scripts/build_desktop.py
uv run --locked python scripts/smoke_desktop.py
uv run --locked python scripts/archive_desktop.py
```

The executable and its bundled libraries live together under `dist/desktop/HCS-Trading-Intelligence`. Keep the entire folder when moving it. Launching the executable without arguments starts the loopback server and opens its dashboard in the default browser. Use `serve` to start without opening a browser. Local application data stays in the operating system's data directory by default. `serve --data-dir PATH` selects a separate directory.

CI builds and smoke-tests the native folder on Windows, macOS and Linux and uploads a compressed archive with a SHA-256 checksum.

A single-file executable is built on every platform (a `.exe` on Windows). After verifying the one-folder build, run the same three Python build, smoke-test and checksum commands with `--onefile`. The executable and checksum are under `dist/desktop-single`. CI uploads the Windows file separately and every platform's file with its MCPB bundle.

The artifacts are development builds. Signing, notarization and production release verification remain separate milestones.

## Release pipeline

Push a candidate commit to a `release/**` branch to run the automated cross-platform build, smoke tests and security checks before tagging. The draft-release workflow runs on version tags.

The native CI matrix installs the checksum-pinned v0.2.0 wheel in a temporary environment, sets an execution pause, then installs the candidate wheel against the same data directory. It checks the persisted pause and its audit record after starting the candidate. A development rebuild can share the baseline version, so the test forces the candidate wheel to be reinstalled. Update the pinned release and digest when changing the upgrade baseline.

CodeQL runs on release branches and again on the tagged source before draft assembly. Download and review its Python and JavaScript/TypeScript SARIF outputs from the same run. A successful scanner job confirms the analysis ran. The release assessment must also record the disposition of its findings.

Tag a commit on `main` as `vX.Y.Z` (or `vX.Y.Z-rc.N`, which is marked pre-release). `.github/workflows/release.yml` then:

1. re-runs every gate in `ci.yml` (lint, types, tests, security scans, native desktop builds) on the tagged commit. The local gate runner `scripts/run_gates.py` also executes `hcs-trader eval-model`; a failing schema, citation or injection case blocks the release,
2. checks that the tag equals the `pyproject.toml` version and is reachable from `main`,
3. builds the wheel and sdist, a CycloneDX SBOM (`scripts/generate_sbom.py`) and smoke-tests both distributions in clean environments, including a Unicode and space data path, the OpenAPI schema and an MCP stdio tool listing,
4. writes `SHA256SUMS` and rejects any wheel or sdist that contains databases, keystores, model files, `.env` files or private-key content (`scripts/verify_release.py`),
5. attests the artifacts, attaches the Sigstore bundle and updates the checksums,
6. creates a **draft** GitHub Release. Review it and publish it by hand.

Verify downloads with `sha256sum --check SHA256SUMS` and verify a built artifact with `gh attestation verify ARTIFACT --repo timfromhcs/hcs-trading-intelligence`. [GitHub artifact attestation documentation](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations)

Pull request checks validate both lockfiles, audit dependencies and check for unreviewed strong-copyleft packages. `pre-commit install` enables the local hooks from `.pre-commit-config.yaml`.

Run `hcs-trader release-check --dist PATH --gates-report REPORT` from the matching candidate checkout. The provenance check requires the GitHub CLI, complete checksums, a CycloneDX SBOM and the Sigstore bundle produced by the draft workflow. It verifies each attested file against the repository, release workflow and checkout commit.

The draft workflow collects desktop artifacts from their designated native CI uploads and the Python distributions from the clean-install job. It attests `build-evidence.json` with the release files. The platform and distribution checks require that signed evidence and compare each artifact's digest and source commit.

## Single entrypoint, selftest and MCPB bundle (Track E)

`hcs-trader` (wheel) and `HCS-Trading-Intelligence` (frozen binary) share one CLI:

| Command | Purpose |
|---|---|
| `serve [--data-dir DIR]` | Loopback dashboard and API |
| `mcp-stdio [--data-dir DIR]` | Read-only MCP server on stdio (alias of `mcp`; creates and migrates a missing database first) |
| `selftest` | Offline installation check; prints one JSON line per check, exits non-zero on any failure |

`selftest` uses a temporary data directory only and checks: database migration, PAPER and offline defaults, kill-switch persistence across a hard `os._exit` of a child process, a real loopback-only socket bind with a health request, and `list_tools` through a stdio subprocess (every listed tool must be declared in the tier table and none may be a live-trade tool). The PAPER check reads the served `/api/v1/system/status`, not just dataclass defaults. Run it on every frozen binary: `scripts/smoke_desktop.py` does so for onedir and onefile builds on all three operating systems.

Verification scripts: `scripts/smoke_wheel.py` (clean-venv install of the wheel and the sdist, the checksum-pinned v0.2.0 upgrade with a persisted kill switch, and `selftest`), `scripts/smoke_desktop.py` (frozen `selftest` plus HTTP smoke) and `tests/test_crash_recovery.py`. `scripts/generate_sbom.py` writes a CycloneDX 1.5 inventory from `uv.lock` and `frontend/package-lock.json`, so it lists the locked dependency graph (runtime and development/build tooling, Python and npm) and does not depend on what happens to be installed on the runner.

### MCPB bundle and MCP registry

`mcpb/manifest.json` (spec 0.3, `server.type = binary`, `platform_overrides.win32` for the `.exe`) describes a bundle that runs `mcp-stdio` from the frozen single-file binary. `python scripts/build_mcpb.py bundle --binary PATH` writes `dist/mcpb/HCS-Trading-Intelligence-<version>-<platform>.mcpb` plus a SHA-256 file. `server.json` is the MCP registry template; `build_mcpb.py registry --bundles DIR` fills the three `fileSha256` values at release time. Manifest and registry shapes are validated offline against subset schemas in `tests/schemas/` (hand-extracted from the upstream specifications, not the full upstream schemas). Nothing is published automatically.

### Workflows

`ci.yml` builds and selftests onedir and onefile binaries, installs wheel and sdist into clean venvs, runs the pinned v0.2.0 upgrade test, checks the manifest and registry template against the project version, and builds the MCPB bundle on Windows, macOS (arm64) and Linux. Its `security` job runs `uv audit`, `pip-audit` on the exported runtime requirements, `npm audit` and gitleaks over the full history; the `verify` job needs it to pass. `release.yml` runs on version tags and calls `ci.yml` as a reusable workflow, so every one of those gates (including the security scans) re-runs on the tagged commit; it also checks the tag against the project version, collects the single-file binaries and MCPB bundles from the native runners, fills `server.json` from the built bundles, and lists every file in `SHA256SUMS`, which is the attested subject. It only creates a draft release. Signing, notarization and registry/PyPI publication remain manual follow-ups.

Unverified assumptions: `macos-latest` is assumed to be arm64 and `ubuntu-latest` x64 (the bundle step names files from the runner's actual platform, and the registry step fails if an expected bundle is missing); no glibc baseline is tested for the Linux binary; the manifest and registry subset schemas in `tests/schemas/` are hand-extracted, so acceptance by a real MCPB client or the registry is not proven.
