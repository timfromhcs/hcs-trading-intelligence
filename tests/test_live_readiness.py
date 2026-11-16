import json
import sqlite3
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from hcs_trader.__main__ import main
from hcs_trader.live_readiness import (
    EVIDENCED,
    UNEVIDENCED,
    Context,
    HardLimits,
    LiveEnableRefused,
    active_live_policy,
    blocking_limitations,
    enable_confirmation_phrase,
    enable_live,
    evaluate_checklist,
    limits_from_mapping,
    limits_history,
    live_state,
    load_known_limitations,
    revoke_live,
    scan_claims,
    validate_limits,
)
from hcs_trader.risk import RiskPolicy
from hcs_trader.storage import AuditStore, KillSwitchCause

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import run_gates  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
LIMITS = HardLimits(
    venue="kraken",
    market="BTC/USD",
    max_hot_wallet_usd=Decimal("500"),
    hot_fraction_of_portfolio=Decimal("0.10"),
    min_notional_usd=Decimal("10"),
    max_notional_usd=Decimal("100"),
    risk_fraction=Decimal("0.0025"),
    max_daily_loss_fraction=Decimal("0.01"),
    max_drawdown_fraction=Decimal("0.05"),
    max_positions=2,
    max_spread_bps=Decimal("50"),
    max_slippage_bps=Decimal("50"),
    cold_reserve_separate_custody=True,
)
CLEAR = "- KL-001 [security-critical] [tested] Evidence linked.\n"
BLOCKING = "- KL-002 [security-critical] [untested] Venue path unproven.\n"


@pytest.fixture
def store(tmp_path: Path) -> AuditStore:
    created = AuditStore(tmp_path / "audit.sqlite")
    created.initialize()
    return created


def register(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "KNOWN_LIMITATIONS.md"
    path.write_text(text, encoding="utf-8")
    return path


def enable(store: AuditStore, path: Path, limits: HardLimits = LIMITS) -> None:
    enable_live(
        store,
        limits,
        operator="op",
        confirmation=enable_confirmation_phrase(limits),
        limitations_path=path,
    )


def test_fresh_install_is_paper(store: AuditStore) -> None:
    assert live_state(store).mode == "PAPER"
    assert active_live_policy(store) is None
    assert RiskPolicy().live_mode_enabled is False


def test_restart_and_update_keep_paper_without_ceremony(tmp_path: Path) -> None:
    first = AuditStore(tmp_path / "a.sqlite")
    first.initialize()
    first.record_startup()
    second = AuditStore(tmp_path / "a.sqlite")
    second.initialize()  # simulates an update's migration run
    assert live_state(second).mode == "PAPER"


def test_ceremony_enables_persists_and_revokes(
    store: AuditStore, tmp_path: Path
) -> None:
    enable(store, register(tmp_path, CLEAR))
    reopened = AuditStore(store.path)
    state = live_state(reopened)
    assert state.mode == "LIVE"
    assert state.limits == LIMITS
    policy = active_live_policy(reopened)
    assert policy is not None
    assert policy.live_mode_enabled
    assert policy.max_notional_usd == Decimal("100")
    assert policy.require_portfolio_state
    assert revoke_live(store, operator="op").mode == "PAPER"
    assert active_live_policy(store) is None
    assert [kind for _, kind, _ in limits_history(store)] == [
        "live.revoked",
        "live.enabled",
    ]


def test_wrong_confirmation_changes_nothing(store: AuditStore, tmp_path: Path) -> None:
    path = register(tmp_path, CLEAR)
    with pytest.raises(LiveEnableRefused, match="confirmation"):
        enable_live(
            store, LIMITS, operator="op", confirmation="yes", limitations_path=path
        )
    other = replace(LIMITS, max_notional_usd=Decimal("200"))
    with pytest.raises(LiveEnableRefused, match="confirmation"):
        enable_live(
            store,
            LIMITS,
            operator="op",
            confirmation=enable_confirmation_phrase(other),
            limitations_path=path,
        )
    assert live_state(store).mode == "PAPER"


def test_untested_security_critical_limitation_blocks_live(
    store: AuditStore, tmp_path: Path
) -> None:
    path = register(tmp_path, CLEAR + BLOCKING)
    assert blocking_limitations(path) == ["KL-002: Venue path unproven."]
    with pytest.raises(LiveEnableRefused, match="KL-002"):
        enable(store, path)
    assert live_state(store).mode == "PAPER"


def test_missing_or_empty_register_fails_closed(
    store: AuditStore, tmp_path: Path
) -> None:
    for path in (tmp_path / "absent.md", register(tmp_path, "no entries\n")):
        with pytest.raises(LiveEnableRefused, match="unreadable"):
            enable(store, path)


def test_kill_switch_and_pause_block_enablement_and_force_paper(
    store: AuditStore, tmp_path: Path
) -> None:
    path = register(tmp_path, CLEAR)
    store.trip_kill_switch(KillSwitchCause.MANUAL, "test trip")
    with pytest.raises(LiveEnableRefused, match="kill switch"):
        enable(store, path)
    store.clear_kill_switch("op", "cleared")
    enable(store, path)
    assert live_state(store).mode == "LIVE"
    store.trip_kill_switch(KillSwitchCause.MANUAL, "test trip")
    assert live_state(store).mode == "PAPER"
    assert active_live_policy(store) is None


def test_every_limit_is_required_and_sane() -> None:
    payload = LIMITS.to_payload()
    assert limits_from_mapping(payload) == LIMITS
    for name in payload:
        trimmed = {k: v for k, v in payload.items() if k != name}
        with pytest.raises(ValueError, match="every field"):
            limits_from_mapping(trimmed)
    with pytest.raises(ValueError, match="every field"):
        limits_from_mapping({**payload, "extra": 1})
    unsafe: list[dict[str, object]] = [
        {"risk_fraction": "0.5"},
        {"max_daily_loss_fraction": "0"},
        {"max_drawdown_fraction": "0.9"},
        {"max_notional_usd": "5000"},
        {"max_notional_usd": "600"},
        {"min_notional_usd": "5"},
        {"max_positions": 0},
        {"max_positions": True},
        {"max_slippage_bps": "NaN"},
        {"max_spread_bps": "abc"},
        {"venue": "binance"},
        {"market": "ETH/USD"},
        {"cold_reserve_separate_custody": False},
        {"cold_reserve_separate_custody": "yes"},
        {"hot_fraction_of_portfolio": "1.5"},
        {"max_drawdown_fraction": "0.005"},
    ]
    for override in unsafe:
        with pytest.raises(ValueError):
            limits_from_mapping({**payload, **override})


def test_invalid_limits_refused_by_ceremony(store: AuditStore, tmp_path: Path) -> None:
    bad = replace(LIMITS, cold_reserve_separate_custody=False)
    with pytest.raises(ValueError, match="separate custody"):
        validate_limits(bad)
    with pytest.raises(LiveEnableRefused, match="hard limits rejected"):
        enable(store, register(tmp_path, CLEAR), bad)


def test_tampered_stored_limits_fail_closed(store: AuditStore, tmp_path: Path) -> None:
    enable(store, register(tmp_path, CLEAR))
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE audit_events SET redacted_args = replace(redacted_args, "
            "'100', '900') WHERE kind = 'live.enabled'"
        )
    state = live_state(AuditStore(store.path))
    assert state.mode == "PAPER"
    assert "fail closed" in state.reason


def test_repository_register_parses_and_currently_blocks_live() -> None:
    register_path = REPO / "docs" / "KNOWN_LIMITATIONS.md"
    assert len(load_known_limitations(register_path)) >= 3
    assert blocking_limitations(register_path)


def test_checklist_is_honest_by_default(tmp_path: Path) -> None:
    report = evaluate_checklist(Context(root=REPO))
    by_id = {item.item_id: item for item in report.items}
    assert by_id["E-04"].status == EVIDENCED
    for gated in ("R-01", "R-02", "D-01", "D-05", "E-05", "E-08"):
        assert by_id[gated].status == UNEVIDENCED
    assert not report.v1_allowed
    assert report.to_json()["unevidenced"] == len(report.unmet)
    empty = evaluate_checklist(Context(root=tmp_path))
    assert empty.items[0].status == UNEVIDENCED  # missing repo docs
    assert "README.md is missing" in empty.claims_findings


def test_checklist_requires_gates_for_the_current_commit() -> None:
    sha = "a" * 40
    gates = {"git_sha": sha, "gates": [{"name": "pytest", "exit_code": 0}]}

    def status(ctx: Context, item_id: str) -> str:
        return {i.item_id: i.status for i in evaluate_checklist(ctx).items}[item_id]

    assert status(Context(root=REPO, gates=gates, head_sha=sha), "R-01") == EVIDENCED
    stale = Context(root=REPO, gates=gates, head_sha="b" * 40)
    assert status(stale, "R-01") == UNEVIDENCED
    failing = {"git_sha": sha, "gates": [{"name": "pytest", "exit_code": 1}]}
    assert (
        status(Context(root=REPO, gates=failing, head_sha=sha), "R-01") == UNEVIDENCED
    )
    assert status(Context(root=REPO, gates=gates, head_sha=sha), "R-02") == UNEVIDENCED


def test_claim_scan_flags_marketing(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("A risk-free, guaranteed profit bot", "utf-8")
    assert len(scan_claims(tmp_path)) == 2


def test_cli_live_status_and_release_check(
    store: AuditStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("hcs_trader.__main__.local_store", lambda: store)
    monkeypatch.setattr("sys.argv", ["hcs-trader", "live-status"])
    main()
    status = json.loads(capsys.readouterr().out)
    assert status["mode"] == "PAPER"
    assert status["blockers"]

    monkeypatch.setattr(
        "sys.argv", ["hcs-trader", "release-check", "--root", str(REPO)]
    )
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 1
    assert json.loads(capsys.readouterr().out)["v1_allowed"] is False


def test_cli_enable_refuses_without_limits(
    store: AuditStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("hcs_trader.__main__.local_store", lambda: store)
    monkeypatch.setattr("sys.argv", ["hcs-trader", "live-enable", "--operator", "op"])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 1
    assert "every hard limit" in capsys.readouterr().err
    assert live_state(store).mode == "PAPER"


def test_cli_enable_reaches_register_gate_and_stays_paper(
    store: AuditStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("hcs_trader.__main__.local_store", lambda: store)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    phrase = enable_confirmation_phrase(LIMITS)
    monkeypatch.setattr("builtins.input", lambda _prompt: phrase)
    argv = ["hcs-trader", "live-enable", "--operator", "op"]
    argv.append("--cold-reserve-separate-custody")
    for name, value in LIMITS.to_payload().items():
        if name not in {"venue", "market", "cold_reserve_separate_custody"}:
            argv += [f"--{name.replace('_', '-')}", str(value)]
    monkeypatch.setattr("sys.argv", argv)
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 1
    assert "known limitations" in capsys.readouterr().err
    assert live_state(store).mode == "PAPER"


def test_parse_pytest_counts_and_retry_budget(tmp_path: Path) -> None:
    assert run_gates.parse_pytest_counts("x\n== 12 passed, 2 skipped in 3s ==") == {
        "passed": 12,
        "skipped": 2,
    }
    assert run_gates.parse_pytest_counts("nothing") == {}
    marker = tmp_path / "count"
    flaky = [
        sys.executable,
        "-c",
        "import pathlib,sys;p=pathlib.Path(sys.argv[1]);"
        "n=int(p.read_text()) if p.exists() else 0;p.write_text(str(n+1));"
        "sys.exit(0 if n>=2 else 1)",
        str(marker),
    ]
    report = run_gates.run_gates((("flaky", flaky),), tmp_path)
    gate = report["gates"][0]  # type: ignore[index]
    assert gate["attempts"] == 3
    assert gate["attempt_exit_codes"] == [1, 1, 0]
    never = [sys.executable, "-c", "raise SystemExit(3)"]
    failed = run_gates.run_gates((("never", never), ("after", never)), tmp_path)
    assert failed["all_passed"] is False
    assert len(failed["gates"]) == 1  # type: ignore[arg-type]
    assert failed["gates"][0]["attempts"] == run_gates.MAX_ATTEMPTS  # type: ignore[index]
