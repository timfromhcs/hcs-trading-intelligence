"""Negative properties: dangerous capabilities must not exist on any surface."""

import ast
import re
from pathlib import Path

from fastapi.testclient import TestClient

from hcs_trader.api import create_app
from hcs_trader.mcp_server import _TOOL_TIERS
from hcs_trader.storage import AuditStore

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "hcs_trader"

FORBIDDEN = re.compile(
    r"seed|mnemonic|private[_-]?key|export[_-]?key|keystore|decrypt|passphrase|"
    r"sign(?!al)|withdraw|sweep|transfer|send[_-]?funds|arbitrary|raw[_-]?call|"
    r"contract[_-]?call|approve|allowance|risk[_-]?limit|limits?\b.*(set|update)|"
    r"disable[_-]?kill|clear[_-]?kill|broadcast",
    re.IGNORECASE,
)


def _rest_routes(tmp_path: Path) -> list[tuple[str, str]]:
    store = AuditStore(tmp_path / "surface.sqlite")
    store.initialize()
    schema = create_app(store).openapi()
    return [
        (path, method)
        for path, methods in schema["paths"].items()
        for method in methods
    ]


def test_rest_surface_has_no_forbidden_routes(tmp_path: Path) -> None:
    routes = _rest_routes(tmp_path)
    assert routes
    offenders = [(p, m) for p, m in routes if FORBIDDEN.search(p)]
    assert offenders == []


def test_rest_mutations_are_an_explicit_allowlist(tmp_path: Path) -> None:
    mutating = {
        path
        for path, method in _rest_routes(tmp_path)
        if method.lower() in {"post", "put", "patch", "delete"}
    }
    allowed = {
        "/api/v1/risk/pause",
        "/api/v1/risk/resume",
        "/api/v1/backtests",
        "/api/v1/backtests/walk-forward",
        "/api/v1/orders/paper",
        "/api/v1/orders/spot",
        "/api/v1/orders/live",
        "/api/v1/orders/preview",
        "/api/v1/system/pause",
        "/api/v1/system/resume",
        "/api/v1/strategies/candidates",
    }
    unexpected = {
        p
        for p in mutating - allowed
        if not p.startswith("/api/v1/strategies/candidates/{candidate_id}/")
    }
    assert unexpected == set()


def test_unknown_dangerous_rest_paths_are_not_routable(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "probe.sqlite")
    with TestClient(create_app(store), base_url="http://127.0.0.1") as client:
        for path in (
            "/api/v1/wallets/export",
            "/api/v1/wallets/seed",
            "/api/v1/keystore/decrypt",
            "/api/v1/sign",
            "/api/v1/withdraw",
            "/api/v1/transfer",
            "/api/v1/risk/limits",
        ):
            for method in ("get", "post", "put", "patch", "delete"):
                assert getattr(client, method)(path).status_code in {404, 405}


def test_mcp_catalogue_has_no_forbidden_tools() -> None:
    assert _TOOL_TIERS
    assert [n for n in _TOOL_TIERS if FORBIDDEN.search(n)] == []
    # Live side-effect tools must stay absent; only paper placement exists.
    assert not [
        n
        for n in _TOOL_TIERS
        if n.startswith(("orders.place", "swaps.")) and n != "orders.place_paper"
    ]
    assert "orders.place_live" not in _TOOL_TIERS


def _imports_decrypt(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module == "hcs_trader.keystore":
            if any(alias.name == "decrypt_keystore_record" for alias in node.names):
                return True
        if isinstance(node, ast.Attribute) and node.attr == "decrypt_keystore_record":
            return True
    return False


def test_only_the_signer_uses_keystore_decryption() -> None:
    users = {
        path.relative_to(PACKAGE).as_posix()
        for path in PACKAGE.rglob("*.py")
        if _imports_decrypt(path.read_text("utf-8"))
    }
    assert users <= {"signing.py"}


def test_transport_modules_never_import_signing_or_keystore() -> None:
    for name in ("mcp_server.py", "local_research.py"):
        tree = ast.parse((PACKAGE / name).read_text("utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not {m for m in imported if m.endswith(("signing", "keystore"))}, name
