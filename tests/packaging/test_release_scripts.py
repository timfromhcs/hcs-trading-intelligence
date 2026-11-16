import json
import tarfile
from pathlib import Path
from zipfile import ZipFile

import generate_sbom
import license_inventory
import pytest
import smoke_desktop
import verify_release
from collect_release import collect

from hcs_trader.live_readiness import (
    EVIDENCED,
    UNEVIDENCED,
    Context,
    evaluate_checklist,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("persistent", [False, True])
def test_windows_onefile_cleanup_retries_locked_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, persistent: bool
) -> None:
    attempts: list[int] = []

    class ExtractionDirectory:
        name = str(tmp_path)

        def cleanup(self) -> None:
            attempts.append(1)
            if persistent or len(attempts) == 1:
                raise PermissionError("DLL still mapped")

    times = iter((0, 1, 11))
    monkeypatch.setattr(
        smoke_desktop, "TemporaryDirectory", lambda **_: ExtractionDirectory()
    )
    monkeypatch.setattr(smoke_desktop, "monotonic", lambda: next(times))
    monkeypatch.setattr(smoke_desktop, "sleep", lambda _: None)
    if persistent:
        with pytest.raises(PermissionError, match="DLL still mapped"):  # noqa: PT012
            with smoke_desktop._temporary_root(wait_for_extracted_files=True):
                pass
    else:
        with smoke_desktop._temporary_root(wait_for_extracted_files=True) as root:
            assert root == tmp_path
    assert len(attempts) == 2


def test_native_release_evidence_uses_smoke_tested_uploads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloads = tmp_path / "collected"
    staging = tmp_path / "staging"
    sha = "a" * 40
    uploads = {
        "desktop-windows-single": ("HCS-Trading-Intelligence.exe",),
        "desktop-windows-latest": ("HCS-Trading-Intelligence-windows-amd64.tar.gz",),
        "desktop-macos-latest": ("HCS-Trading-Intelligence-darwin-arm64.tar.gz",),
        "desktop-ubuntu-latest": ("HCS-Trading-Intelligence-linux-x86_64.tar.gz",),
        "release-python": (
            "hcs_trader-1.0.0-py3-none-any.whl",
            "hcs_trader-1.0.0.tar.gz",
            "sbom.cdx.json",
        ),
    }
    for upload, names in uploads.items():
        folder = downloads / upload
        folder.mkdir(parents=True)
        for name in names:
            (folder / name).write_text(name, encoding="ascii")
    collect(downloads, staging, sha)
    evidence = json.loads((staging / "build-evidence.json").read_text())
    assert evidence["commit"] == sha
    assert len(evidence["artifacts"]) == 4

    platform_items = {"D-01", "D-02", "D-03", "D-04"}

    def statuses(head: str) -> dict[str, str]:
        return {
            item.item_id: item.status
            for item in evaluate_checklist(
                Context(root=ROOT, dist=staging, head_sha=head)
            ).items
            if item.item_id in platform_items
        }

    assert statuses(sha) == dict.fromkeys(platform_items, UNEVIDENCED)
    monkeypatch.setattr(
        "hcs_trader.live_readiness._chk_checksums", lambda _: (True, "verified fixture")
    )
    assert statuses(sha) == dict.fromkeys(platform_items, EVIDENCED)
    assert statuses("b" * 40) == dict.fromkeys(platform_items, UNEVIDENCED)
    (staging / "HCS-Trading-Intelligence.exe").write_bytes(b"different")
    assert statuses(sha)["D-01"] == UNEVIDENCED

    linux_name = uploads["desktop-ubuntu-latest"][0]
    linux = downloads / "desktop-ubuntu-latest" / linux_name
    linux.rename(downloads / "desktop-macos-latest" / linux_name)
    with pytest.raises(ValueError, match="desktop-ubuntu-latest"):
        collect(downloads, tmp_path / "wrong-runner", sha)


def test_sbom_shape_from_real_lockfiles() -> None:
    sbom = generate_sbom.build_sbom(
        ROOT / "uv.lock",
        ROOT / "frontend" / "package-lock.json",
        name="hcs-trader",
        version="9.9.9",
    )
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.5"
    assert sbom["metadata"]["component"]["version"] == "9.9.9"
    components = sbom["components"]
    assert components
    purls = [c["purl"] for c in components]
    assert purls == sorted(purls)
    assert any(p.startswith("pkg:pypi/") for p in purls)
    assert any(p.startswith("pkg:npm/") for p in purls)
    assert not any(p.startswith("pkg:pypi/hcs-trader@") for p in purls)
    assert all({"type", "name", "version", "purl"} <= set(c) for c in components)
    json.dumps(sbom)


def test_sbom_from_minimal_lockfiles(tmp_path: Path) -> None:
    uv = tmp_path / "uv.lock"
    uv.write_text(
        '[[package]]\nname = "demo"\nversion = "1.0"\n'
        'sdist = { hash = "sha256:' + "a" * 64 + '" }\n'
        '[[package]]\nname = "hcs-trader"\nversion = "0.2.0"\n',
        encoding="utf-8",
    )
    npm = tmp_path / "package-lock.json"
    npm.write_text(
        json.dumps(
            {
                "packages": {
                    "": {"name": "root"},
                    "node_modules/@scope/pkg": {"version": "2.0.0", "license": "MIT"},
                }
            }
        ),
        encoding="utf-8",
    )
    sbom = generate_sbom.build_sbom(uv, npm, name="hcs-trader", version="0.2.0")
    names = {c["name"] for c in sbom["components"]}
    assert names == {"demo", "@scope/pkg"}
    demo = next(c for c in sbom["components"] if c["name"] == "demo")
    assert demo["hashes"] == [{"alg": "SHA-256", "content": "a" * 64}]


def test_checksums_round_trip_and_tamper_detection(tmp_path: Path) -> None:
    (tmp_path / "a.whl").write_bytes(b"one")
    (tmp_path / "b.tar.gz").write_bytes(b"two")
    (tmp_path / "b.tar.gz.sha256").write_text("ignored", encoding="ascii")
    verify_release.write_sums(tmp_path)
    lines = (tmp_path / "SHA256SUMS").read_text(encoding="ascii").splitlines()
    assert len(lines) == 2
    assert verify_release.verify_sums(tmp_path) == []
    (tmp_path / "a.whl").write_bytes(b"tampered")
    assert verify_release.verify_sums(tmp_path) == ["a.whl checksum mismatch"]
    (tmp_path / "c.exe").write_bytes(b"new")
    assert "c.exe is not covered by SHA256SUMS" in verify_release.verify_sums(tmp_path)
    (tmp_path / "a.whl").unlink()
    assert "a.whl is listed but missing" in verify_release.verify_sums(tmp_path)


def test_verify_sums_requires_file_and_rejects_malformed(tmp_path: Path) -> None:
    assert verify_release.verify_sums(tmp_path) == ["SHA256SUMS is missing"]
    (tmp_path / "SHA256SUMS").write_text("nothex  file\n", encoding="ascii")
    assert verify_release.verify_sums(tmp_path)[0].startswith("malformed")
    with pytest.raises(RuntimeError):
        verify_release.write_sums(tmp_path / ".")


def _wheel(path: Path, files: dict[str, bytes]) -> Path:
    with ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


@pytest.mark.parametrize(
    "name",
    [
        "hcs_trader/audit.sqlite",
        "hcs_trader/model.gguf",
        "hcs_trader/.env",
        "hcs_trader/.env.local",
        "hcs_trader/keystore.json",
        "hcs_trader/server.pem",
        "id_ed25519",
    ],
)
def test_wheel_with_forbidden_file_is_rejected(tmp_path: Path, name: str) -> None:
    wheel = _wheel(tmp_path / "x.whl", {name: b"data", "hcs_trader/ok.py": b"x=1"})
    assert verify_release.check_distribution(wheel)


def test_wheel_with_private_key_content_is_rejected(tmp_path: Path) -> None:
    marker = b"-----BEGIN " + b"EC PRIVATE KEY-----"
    wheel = _wheel(tmp_path / "x.whl", {"hcs_trader/a.py": marker})
    assert verify_release.check_distribution(wheel)


def test_clean_wheel_and_sdist_pass(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path / "x.whl", {"hcs_trader/keystore.py": b"x = 1"})
    assert verify_release.check_distribution(wheel) == []
    sdist = tmp_path / "x.tar.gz"
    source = tmp_path / "mod.py"
    source.write_text("x = 1", encoding="ascii")
    with tarfile.open(sdist, "w:gz") as archive:
        archive.add(source, "x/mod.py")
    assert verify_release.check_distribution(sdist) == []
    with pytest.raises(ValueError):
        verify_release.check_distribution(tmp_path / "x.zip")


@pytest.mark.parametrize(
    ("expression", "flagged"),
    [
        ("MIT", False),
        ("Apache-2.0", False),
        ("LGPL-3.0-or-later", False),
        ("GPL-3.0-only", True),
        ("AGPL-3.0-or-later", True),
        ("GPL-2.0-or-later WITH Bootloader-exception", False),
        ("GPL-2.0-or-later OR Apache-2.0", False),
        ("GPL-2.0-or-later OR AGPL-3.0-only", True),
    ],
)
def test_copyleft_gate(expression: str, flagged: bool) -> None:
    record = {
        "name": "pkg",
        "version": "1",
        "spdx_expression": expression,
        "legacy_classifiers": [],
    }
    assert bool(license_inventory.copyleft_violations([record])) is flagged
    assert license_inventory.copyleft_violations([record], frozenset({"pkg"})) == []


def test_installed_environment_has_no_unreviewed_copyleft() -> None:
    assert license_inventory.copyleft_violations(license_inventory.collect()) == []


def test_pyinstaller_exception_is_bound_to_the_reviewed_version() -> None:
    record = {
        "name": "PyInstaller",
        "version": "6.22.3",
        "spdx_expression": "GPL-2.0-only",
        "legacy_classifiers": [],
    }
    assert license_inventory.copyleft_violations([record]) == []
    assert license_inventory.copyleft_violations([{**record, "version": "6.22.4"}])
