"""Offline validation of the MCPB manifest, bundle builder and registry JSON."""

import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = Path(__file__).parent / "schemas"


def _load_builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "build_mcpb", ROOT / "scripts" / "build_mcpb.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_mcpb"] = module
    spec.loader.exec_module(module)
    return module


def _validate(instance: object, schema_name: str) -> None:
    jsonschema = importlib.import_module("jsonschema")
    schema = json.loads((SCHEMAS / schema_name).read_text("utf-8"))
    jsonschema.Draft7Validator.check_schema(schema)
    jsonschema.Draft7Validator(schema).validate(instance)


def test_manifest_matches_offline_schema_and_is_read_only_binary() -> None:
    manifest = json.loads((ROOT / "mcpb" / "manifest.json").read_text("utf-8"))
    _validate(manifest, "mcpb-manifest-0.3.subset.schema.json")
    config = manifest["server"]["mcp_config"]
    assert manifest["server"]["type"] == "binary"
    assert config["args"] == ["mcp-stdio"]
    assert config["platform_overrides"]["win32"]["command"].endswith(".exe")
    assert "${__dirname}" in config["command"]
    assert "user_config" not in manifest  # no secrets or settings collected


def test_manifest_and_registry_versions_follow_pyproject() -> None:
    _load_builder().check_consistency()


def test_manifest_rejects_missing_server(tmp_path: Path) -> None:
    manifest = json.loads((ROOT / "mcpb" / "manifest.json").read_text("utf-8"))
    del manifest["server"]
    with pytest.raises(Exception, match="server"):
        _validate(manifest, "mcpb-manifest-0.3.subset.schema.json")


def test_bundle_and_registry_are_built_offline(tmp_path: Path) -> None:
    builder = _load_builder()
    binary = tmp_path / "fake-binary"
    binary.write_bytes(b"not a real executable")
    bundle = builder.build_bundle(binary, tmp_path / "out")
    with ZipFile(bundle) as archive:
        names = archive.namelist()
        assert "manifest.json" in names
        entry = next(name for name in names if name.startswith("server/"))
        assert entry.startswith("server/HCS-Trading-Intelligence")
        assert archive.read(entry) == b"not a real executable"
        _validate(
            json.loads(archive.read("manifest.json")),
            "mcpb-manifest-0.3.subset.schema.json",
        )
    assert bundle.with_name(bundle.name + ".sha256").is_file()

    bundles = tmp_path / "all"
    bundles.mkdir()
    version = builder.project_version()
    for tag in builder.PLATFORM_TAGS:
        (bundles / f"HCS-Trading-Intelligence-{version}-{tag}.mcpb").write_bytes(
            tag.encode()
        )
    output = tmp_path / "server.json"
    builder.fill_registry(bundles, output)
    document = json.loads(output.read_text("utf-8"))
    _validate(document, "mcp-registry-server.subset.schema.json")
    assert len({p["fileSha256"] for p in document["packages"]}) == 3
    assert "${" not in output.read_text("utf-8")


def test_registry_template_is_not_valid_until_filled() -> None:
    template = json.loads((ROOT / "server.json").read_text("utf-8"))
    with pytest.raises(Exception, match="fileSha256|does not match"):
        _validate(template, "mcp-registry-server.subset.schema.json")
