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


def violations(
    source: str, module: str = "hcs_trader.x", allow: frozenset[str] = frozenset()
) -> list[str]:
    """Forbidden imports in ``source``; ``allow`` lists exact-prefix exceptions."""
    bad = []
    for name in sorted(imported_modules(source, module)):
        root = name.split(".")[0]
        if any(name == ok or name.startswith(ok + ".") for ok in allow):
            continue
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


# Signing, key-handling and execution modules. They are not the pure domain layer: each
# may import exactly the named dependencies below (measured on the current code) and
# nothing else from the forbidden set. In particular none may import an HTTP client,
# the MCP SDK, the local model, the API or the MCP server. Adding an entry here is a
# security review decision, never a way to make a test pass.
SIGNING_AND_EXECUTION_ALLOWED: dict[str, frozenset[str]] = {
    "evm_signing.py": frozenset(),
    "solana_signing.py": frozenset({"solders"}),  # key and message codec, no network
    "utxo_psbt.py": frozenset({"hcs_trader.wallets"}),  # network table and RPC port
    "keystore.py": frozenset(),
    "spot_port.py": frozenset(),
    "spot_execution.py": frozenset(
        {
            "hcs_trader.data.kraken",
            "hcs_trader.data.provider",
            "hcs_trader.kraken_spot",
            "hcs_trader.storage",
        }
    ),
    "paper_execution.py": frozenset(
        {"hcs_trader.data.kraken", "hcs_trader.data.quotes", "hcs_trader.storage"}
    ),
}
NEVER_ALLOWED = frozenset(
    {
        "httpx",
        "httpx2",
        "mcp",
        "requests",
        "aiohttp",
        "hcs_trader.local_research",
        "hcs_trader.mcp_server",
        "hcs_trader.api",
    }
)


def _module_name(path: Path) -> str:
    return ".".join(["hcs_trader", *path.relative_to(PACKAGE).with_suffix("").parts])


@pytest.mark.parametrize("name", sorted(SIGNING_AND_EXECUTION_ALLOWED))
def test_signing_and_execution_modules_stay_off_network_and_llm_adapters(
    name: str,
) -> None:
    path = PACKAGE / name
    allow = SIGNING_AND_EXECUTION_ALLOWED[name]
    assert not allow & NEVER_ALLOWED
    source = path.read_text(encoding="utf-8")
    assert violations(source, _module_name(path), allow) == []
    # Never reachable even through an allowed prefix.
    for imported in imported_modules(source, _module_name(path)):
        assert imported.split(".")[0] not in NEVER_ALLOWED
        assert not any(
            imported == bad or imported.startswith(bad + ".") for bad in NEVER_ALLOWED
        )


def test_allowance_table_covers_every_signing_and_execution_module() -> None:
    present = {
        path.name
        for path in PACKAGE.glob("*.py")
        if path.stem.endswith(("_signing", "_execution", "_psbt"))
        or path.stem in {"signing", "keystore", "spot_port"}
    }
    # signing.py is guarded by PROTECTED above; the rest must be in the table.
    assert present - {"signing.py"} <= set(SIGNING_AND_EXECUTION_ALLOWED)


def test_allowances_are_effective_and_not_blanket() -> None:
    assert violations("from hcs_trader.storage import AuditStore")
    allow = frozenset({"hcs_trader.storage"})
    assert violations("from hcs_trader.storage import AuditStore", allow=allow) == []
    assert violations("import httpx2", allow=allow)
    assert violations("from hcs_trader import mcp_server", allow=allow)
