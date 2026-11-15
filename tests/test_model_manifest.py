import json
import sys
from hashlib import sha256
from pathlib import Path

import pytest

from hcs_trader import __main__ as launcher
from hcs_trader.model_manifest import verify_model_manifest
from hcs_trader.storage import AuditStore


def test_research_manifest_checks_file_before_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gguf = tmp_path / "fixture.gguf"
    content = b"GGUF" + bytes(24)
    gguf.write_bytes(content)
    digest = sha256(content).hexdigest()
    manifest = tmp_path / "model.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "gguf": gguf.name,
                "metadata": {
                    "alias": "fixture",
                    "sha256": digest,
                    "license": "MIT",
                    "quantization": "Q4_K_M",
                    "runtime_version": "llama.cpp fixture",
                },
            }
        ),
        encoding="utf-8",
    )
    assert verify_model_manifest(manifest).sha256 == digest

    token_file = tmp_path / "token"
    token_file.write_text("fixture-token", encoding="utf-8")
    monkeypatch.setattr(
        launcher, "local_store", lambda: AuditStore(tmp_path / "research.sqlite")
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hcs-trader",
            "research-brief",
            "--model-manifest",
            str(manifest),
            "--token-file",
            str(token_file),
        ],
    )
    launcher.main()
    output = capsys.readouterr().out
    assert "fixture-token" not in output
    report = json.loads(output)
    assert report["status"] == "insufficient_data"
    assert report["execution_decision"] == "NO_TRADE"
    assert report["verified_artifact"]["sha256"] == digest

    gguf.write_bytes(content + b"x")
    with pytest.raises(ValueError, match="declared SHA-256"):
        verify_model_manifest(manifest)
