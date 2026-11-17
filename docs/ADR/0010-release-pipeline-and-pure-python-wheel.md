# ADR 0010: Tag-triggered draft release and pure-Python wheel

## Status

Accepted.

## Context

AGENTS.md section 15 asks for native-runner builds, wheels, checksums, an SBOM and provenance, and section 15.3 names `cibuildwheel` for wheels that contain native extensions.

## Decision

- `hcs-trader` is a pure-Python distribution (`py3-none-any` wheel plus sdist). It has no native extension, so `cibuildwheel` adds no value and is not used. CI instead installs the wheel and the sdist in clean environments (`scripts/smoke_wheel.py`) and runs the tests on a second interpreter (Python 3.13). Revisit this ADR if a native extension is ever added.
- Desktop executables are built on native runners by `ci.yml`. `release.yml` is triggered only by a version tag, re-runs `ci.yml` through `workflow_call` on the tagged commit, checks that the tag matches `pyproject.toml` and is reachable from `main`, then creates a **draft** GitHub Release containing the wheel, sdist, desktop archives, a CycloneDX SBOM and an aggregate `SHA256SUMS`. Only the final job holds `contents: write`.
- Nothing is uploaded to PyPI by automation. Publishing a draft is a deliberate maintainer action.
- The draft workflow uses the checksum manifest as input to a SHA-pinned GitHub attestation action. It attaches the resulting Sigstore bundle before regenerating the release manifest. Only the final job can request an OIDC token and write an attestation.

## Consequences

- A failed gate, version mismatch, checksum mismatch or secret-like file in a distribution stops the release before a draft exists.
- Desktop builds remain unsigned development builds. Signing and notarization stay separate milestones.
