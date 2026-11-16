"""Generate a CycloneDX 1.5 SBOM from uv.lock and frontend/package-lock.json."""

import argparse
import json
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import quote


def _purl_pypi(name: str, version: str) -> str:
    return f"pkg:pypi/{quote(name.lower().replace('_', '-'))}@{quote(version)}"


def _purl_npm(name: str, version: str) -> str:
    return f"pkg:npm/{quote(name, safe='@/')}@{quote(version)}"


def python_components(lock: Path, root_name: str) -> list[dict[str, Any]]:
    data = tomllib.loads(lock.read_text(encoding="utf-8"))
    components: list[dict[str, Any]] = []
    for package in data.get("package", []):
        name, version = package["name"], package.get("version")
        if name == root_name or version is None:
            continue
        component: dict[str, Any] = {
            "type": "library",
            "name": name,
            "version": version,
            "purl": _purl_pypi(name, version),
        }
        sdist = package.get("sdist") or {}
        digest = str(sdist.get("hash", ""))
        if digest.startswith("sha256:"):
            component["hashes"] = [{"alg": "SHA-256", "content": digest[7:]}]
        components.append(component)
    return components


def npm_components(lock: Path) -> list[dict[str, Any]]:
    data = json.loads(lock.read_text(encoding="utf-8"))
    components: list[dict[str, Any]] = []
    for path, package in data.get("packages", {}).items():
        if not path or "version" not in package:
            continue
        name = package.get("name") or path.rsplit("node_modules/", 1)[-1]
        component: dict[str, Any] = {
            "type": "library",
            "name": name,
            "version": package["version"],
            "purl": _purl_npm(name, package["version"]),
        }
        if package.get("license"):
            component["licenses"] = [{"license": {"name": str(package["license"])}}]
        components.append(component)
    return components


def build_sbom(
    uv_lock: Path, npm_lock: Path, *, name: str, version: str
) -> dict[str, Any]:
    components = python_components(uv_lock, name.replace("_", "-"))
    components += npm_components(npm_lock)
    components.sort(key=lambda item: item["purl"])
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {"type": "application", "name": name, "version": version}
        },
        "components": components,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv-lock", type=Path, default=Path("uv.lock"))
    parser.add_argument(
        "--npm-lock", type=Path, default=Path("frontend/package-lock.json")
    )
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--output", type=Path, default=Path("dist/sbom.cdx.json"))
    args = parser.parse_args()
    project = tomllib.loads(args.pyproject.read_text(encoding="utf-8"))["project"]
    sbom = build_sbom(
        args.uv_lock, args.npm_lock, name=project["name"], version=project["version"]
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(sbom, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(sbom['components'])} components to {args.output}")


if __name__ == "__main__":
    main()
