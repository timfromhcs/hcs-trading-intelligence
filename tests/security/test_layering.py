"""Import-layering rules: deterministic safety code never reaches adapters."""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "hcs_trader"

# Protected layers: the deterministic risk, domain and signing boundaries.
PROTECTED = ["risk.py", "signing.py", "domain"]

# Third-party network/LLM/transport libraries and project adapters they must avoid.
FORBIDDEN_EXTERNAL = {
    "httpx",
    "httpx2",
    "requests",
    "aiohttp",
    "urllib3",
    "websockets",
    "socket",
    "http",
    "urllib",
    "uvicorn",
    "fastapi",
    "mcp",
    "openai",
    "anthropic",
    "llama_cpp",
    "ccxt",
    "web3",
    "solders",
}
FORBIDDEN_INTERNAL = {
    "hcs_trader.local_research",
    "hcs_trader.mcp_server",
    "hcs_trader.api",
    "hcs_trader.data",
    "hcs_trader.kraken_spot",
    "hcs_trader.spot_execution",
    "hcs_trader.jupiter_swap",
    "hcs_trader.evm_swap",
    "hcs_trader.wallets",
    "hcs_trader.paper_execution",
    "hcs_trader.storage",
}


def imported_modules(source: str, module: str = "hcs_trader.x") -> set[str]:
    """Return every absolute module name imported anywhere in the source."""
    found: set[str] = set()
    package = module.rsplit(".", 1)[0]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")[: len(package.split(".")) - node.level + 1]
                base = ".".join([*parts, base] if base else parts)
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return found


def violations(source: str, module: str = "hcs_trader.x") -> list[str]:
    bad = []
    for name in sorted(imported_modules(source, module)):
        root = name.split(".")[0]
        if root in FORBIDDEN_EXTERNAL or any(
            name == target or name.startswith(target + ".")
            for target in FORBIDDEN_INTERNAL
        ):
            bad.append(name)
    return bad


def _protected_files() -> list[Path]:
    files: list[Path] = []
    for entry in PROTECTED:
        path = PACKAGE / entry
        files += sorted(path.rglob("*.py")) if path.is_dir() else [path]
    return files


def test_protected_layers_exist() -> None:
    files = _protected_files()
    assert files
    assert all(path.is_file() for path in files)


@pytest.mark.parametrize(
    "path", _protected_files(), ids=lambda p: p.relative_to(PACKAGE).as_posix()
)
def test_protected_layer_imports_no_network_or_llm_adapters(path: Path) -> None:
    relative = path.relative_to(PACKAGE).with_suffix("")
    module = ".".join(["hcs_trader", *relative.parts])
    assert violations(path.read_text(encoding="utf-8"), module) == []


@pytest.mark.parametrize(
    "source",
    [
        "import httpx",
        "from mcp import Client",
        "from hcs_trader.local_research import ResearchRun",
        "def f():\n    from hcs_trader.kraken_spot import KrakenSpot",
        "from hcs_trader import jupiter_swap",
        "from hcs_trader.data.kraken import BTC_USD",
        "import urllib.request",
    ],
)
def test_checker_detects_forbidden_imports(source: str) -> None:
    assert violations(source)


def test_checker_resolves_relative_imports() -> None:
    assert violations("from . import evm_swap", "hcs_trader.risk")
    assert violations("from .domain import execution", "hcs_trader.risk") == []
