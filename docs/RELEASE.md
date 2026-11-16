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

Windows also supports a single EXE. After verifying the one-folder build, run the same three Python build, smoke-test and checksum commands with `--onefile`. The executable and checksum are under `dist/desktop-single`. Windows CI uploads these files separately.

The artifacts are development builds. Signing, notarization and production release verification remain separate milestones.

## Release pipeline

Push a candidate commit to a `release/**` branch to run the automated cross-platform build, smoke tests and security checks before tagging. The draft-release workflow runs on version tags.

The native CI matrix installs the checksum-pinned v0.2.0 wheel in a temporary environment, sets an execution pause, then installs the candidate wheel against the same data directory. It checks the persisted pause and its audit record after starting the candidate. A development rebuild can share the baseline version, so the test forces the candidate wheel to be reinstalled. Update the pinned release and digest when changing the upgrade baseline.

Tag a commit on `main` as `vX.Y.Z` (or `vX.Y.Z-rc.N`, which is marked pre-release). `.github/workflows/release.yml` then:

1. re-runs every gate in `ci.yml` (lint, types, tests, security scans, native desktop builds) on the tagged commit,
2. checks that the tag equals the `pyproject.toml` version and is reachable from `main`,
3. builds the wheel and sdist, a CycloneDX SBOM (`scripts/generate_sbom.py`) and smoke-tests both distributions in clean environments, including a Unicode and space data path, the OpenAPI schema and an MCP stdio tool listing,
4. writes `SHA256SUMS` and rejects any wheel or sdist that contains databases, keystores, model files, `.env` files or private-key content (`scripts/verify_release.py`),
5. attests the artifacts, attaches the Sigstore bundle and updates the checksums,
6. creates a **draft** GitHub Release. Review it and publish it by hand.

Verify downloads with `sha256sum --check SHA256SUMS` and verify a built artifact with `gh attestation verify ARTIFACT --repo timfromhcs/hcs-trading-intelligence`. [GitHub artifact attestation documentation](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations)

Pull request checks validate both lockfiles, audit dependencies and check for unreviewed strong-copyleft packages. `pre-commit install` enables the local hooks from `.pre-commit-config.yaml`.

Run `hcs-trader release-check --dist PATH --gates-report REPORT` from the matching candidate checkout. The provenance check requires the GitHub CLI, complete checksums, a CycloneDX SBOM and the Sigstore bundle produced by the draft workflow. It verifies each attested file against the repository, release workflow and checkout commit.

The draft workflow collects desktop artifacts from their designated native CI uploads and the Python distributions from the clean-install job. It attests `build-evidence.json` with the release files. The platform and distribution checks require that signed evidence and compare each artifact's digest and source commit.
