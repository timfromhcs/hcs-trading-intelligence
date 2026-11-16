import json
import tarfile
from pathlib import Path
from zipfile import ZipFile

import generate_sbom
import license_inventory
import pytest
import verify_release

ROOT = Path(__file__).resolve().parents[2]


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
