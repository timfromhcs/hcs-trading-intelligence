# ruff: noqa: E501
"""LIVE enablement ceremony, persisted hard limits and V1 section 18 checklist.

PAPER is the default. LIVE exists only while the newest ceremony event in the local
audit log is an enablement whose operator-set hard limits are all present, sane and
hash-verified. Anything missing, corrupt or unreadable resolves to PAPER.
Section 18 items are reported ``evidenced`` only when machine evidence exists;
everything else stays ``unevidenced``.
"""

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from fnmatch import fnmatchcase
from hashlib import file_digest
from pathlib import Path
from typing import Any

from hcs_trader.domain.signing import signing_capabilities
from hcs_trader.risk import RiskPolicy
from hcs_trader.storage import AuditStore, canonical_hash

KIND_ENABLED = "live.enabled"
KIND_REVOKED = "live.revoked"
_CEREMONY_KINDS = frozenset({KIND_ENABLED, KIND_REVOKED})
_HISTORY_WINDOW = 500
_SCOPE = "ADMIN_LOCAL"
_ZERO = Decimal(0)


class LiveEnableRefused(Exception):
    """The LIVE ceremony was refused; the system remains in PAPER."""


@dataclass(frozen=True, slots=True)
class HardLimits:
    """Operator-set limits; every field is required (no defaults on purpose)."""

    venue: str
    market: str
    max_hot_wallet_usd: Decimal
    hot_fraction_of_portfolio: Decimal
    min_notional_usd: Decimal
    max_notional_usd: Decimal
    risk_fraction: Decimal
    max_daily_loss_fraction: Decimal
    max_drawdown_fraction: Decimal
    max_positions: int
    max_spread_bps: Decimal
    max_slippage_bps: Decimal
    cold_reserve_separate_custody: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in asdict(self).items()
        }

    @property
    def policy_version(self) -> str:
        return f"live-{canonical_hash(self.to_payload())[:12]}"


def _dec(value: object, name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{name} is not a number") from None
    if not number.is_finite():
        raise ValueError(f"{name} must be finite")
    return number


def _bounded(value: Decimal, name: str, low: Decimal, high: Decimal) -> None:
    if not low < value <= high:
        raise ValueError(f"{name} must be above {low} and at most {high}")


def validate_limits(limits: HardLimits) -> None:
    """Sanity-check every threshold; raises ValueError naming the first problem."""
    policy = RiskPolicy()
    if limits.venue not in policy.allowed_venues:
        raise ValueError(f"venue '{limits.venue}' is not an executable venue")
    if limits.market not in policy.allowed_markets:
        raise ValueError(f"market '{limits.market}' is not an executable market")
    if not limits.cold_reserve_separate_custody:
        raise ValueError(
            "confirm the cold reserve is held in separate custody "
            "(a software percentage does not isolate it)"
        )
    fields: tuple[tuple[str, Decimal, Decimal], ...] = (
        ("max_hot_wallet_usd", limits.max_hot_wallet_usd, Decimal(1_000_000)),
        ("hot_fraction_of_portfolio", limits.hot_fraction_of_portfolio, Decimal(1)),
        ("max_notional_usd", limits.max_notional_usd, Decimal(1000)),
        ("risk_fraction", limits.risk_fraction, Decimal("0.01")),
        ("max_daily_loss_fraction", limits.max_daily_loss_fraction, Decimal("0.05")),
        ("max_drawdown_fraction", limits.max_drawdown_fraction, Decimal("0.20")),
        ("max_spread_bps", limits.max_spread_bps, Decimal(500)),
        ("max_slippage_bps", limits.max_slippage_bps, Decimal(500)),
    )
    for name, value, high in fields:
        _bounded(_dec(value, name), name, _ZERO, high)
    if (
        not 10
        <= _dec(limits.min_notional_usd, "min_notional_usd")
        <= limits.max_notional_usd
    ):
        raise ValueError("min_notional_usd must be between 10 and max_notional_usd")
    if limits.max_notional_usd > limits.max_hot_wallet_usd:
        raise ValueError("max_notional_usd cannot exceed max_hot_wallet_usd")
    if limits.max_drawdown_fraction < limits.max_daily_loss_fraction:
        raise ValueError("max_drawdown_fraction cannot be below the daily loss limit")
    if not 1 <= limits.max_positions <= 5:  # noqa: PLR2004
        raise ValueError("max_positions must be between 1 and 5")


def limits_from_mapping(raw: dict[str, Any]) -> HardLimits:
    """Strict parse: every field required, no extras, then full validation."""
    expected = set(HardLimits.__dataclass_fields__)
    if set(raw) != expected:
        missing = sorted(expected - set(raw))
        extra = sorted(set(raw) - expected)
        raise ValueError(
            f"limits must set every field (missing={missing}, extra={extra})"
        )
    decimals = {
        name
        for name, field_def in HardLimits.__dataclass_fields__.items()
        if field_def.type is Decimal
    }
    values: dict[str, Any] = {
        name: _dec(value, name) if name in decimals else value
        for name, value in raw.items()
    }
    if not isinstance(values["max_positions"], int) or isinstance(
        values["max_positions"], bool
    ):
        raise ValueError("max_positions must be an integer")
    if not isinstance(values["cold_reserve_separate_custody"], bool):
        raise ValueError("cold_reserve_separate_custody must be true or false")
    limits = HardLimits(**values)
    validate_limits(limits)
    return limits


@dataclass(frozen=True, slots=True)
class LiveState:
    mode: str  # "PAPER" or "LIVE"
    reason: str
    limits: HardLimits | None = None
    policy_version: str | None = None


def live_state(store: AuditStore) -> LiveState:
    """Resolve the persisted ceremony state; every doubt resolves to PAPER."""
    events = store.list_structured_audit(_HISTORY_WINDOW)
    event = next((e for e in events if e.kind in _CEREMONY_KINDS), None)
    if event is None:
        return LiveState("PAPER", "no live enablement recorded (default)")
    if event.kind == KIND_REVOKED:
        return LiveState("PAPER", "live enablement revoked by operator")
    try:
        raw = json.loads(event.redacted_args or "")
        limits = limits_from_mapping(raw)
        if (
            event.args_hash != canonical_hash(limits.to_payload())
            or event.policy_version != limits.policy_version
        ):
            raise ValueError("stored limits do not match their recorded hash")
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        return LiveState("PAPER", f"stored live limits invalid ({error}); fail closed")
    if store.fail_safe_paused():
        return LiveState(
            "PAPER",
            "execution is paused or kill switch active",
            limits,
            limits.policy_version,
        )
    return LiveState(
        "LIVE", "operator enablement active", limits, limits.policy_version
    )


def limits_history(store: AuditStore) -> list[tuple[str, str, str | None]]:
    """Changelog of ceremony events (recorded_at, kind, policy_version), newest first."""
    return [
        (e.recorded_at, e.kind, e.policy_version)
        for e in store.list_structured_audit(_HISTORY_WINDOW)
        if e.kind in _CEREMONY_KINDS
    ]


def active_live_policy(store: AuditStore) -> RiskPolicy | None:
    """The RiskPolicy to enforce in LIVE, or None while the system is in PAPER."""
    state = live_state(store)
    if state.mode != "LIVE" or state.limits is None:
        return None
    lim = state.limits
    return RiskPolicy(
        live_mode_enabled=True,
        allowed_venues=frozenset({lim.venue}),
        allowed_markets=frozenset({lim.market}),
        min_notional_usd=lim.min_notional_usd,
        max_notional_usd=lim.max_notional_usd,
        max_spread_bps=lim.max_spread_bps,
        max_slippage_bps=lim.max_slippage_bps,
        require_portfolio_state=True,
        risk_fraction=lim.risk_fraction,
        max_daily_loss_fraction=lim.max_daily_loss_fraction,
        max_drawdown_fraction=lim.max_drawdown_fraction,
        max_positions=lim.max_positions,
        max_hot_wallet_usd=lim.max_hot_wallet_usd,
        hot_fraction_of_portfolio=lim.hot_fraction_of_portfolio,
    )


# --- known limitations (AGENTS.md 13.3) ---

_LIMITATION_LINE = re.compile(
    r"^- (KL-\d{3}) \[(security-critical|other)\] \[(untested|tested|accepted)\] (.+)$"
)


@dataclass(frozen=True, slots=True)
class Limitation:
    limitation_id: str
    category: str
    status: str
    text: str

    @property
    def blocks_live(self) -> bool:
        return self.category == "security-critical" and self.status == "untested"


def load_known_limitations(path: Path) -> list[Limitation]:
    """Parse the limitations register; raises OSError/ValueError (fail closed)."""
    found = [
        Limitation(*match.groups())
        for line in path.read_text(encoding="utf-8").splitlines()
        if (match := _LIMITATION_LINE.match(line))
    ]
    if not found:
        raise ValueError(f"{path.name} lists no limitations; the register is required")
    return found


def blocking_limitations(path: Path) -> list[str]:
    """Reasons LIVE is blocked by the register; an unreadable register blocks."""
    try:
        entries = load_known_limitations(path)
    except (OSError, ValueError) as error:
        return [f"known-limitations register unreadable: {error}"]
    return [f"{e.limitation_id}: {e.text}" for e in entries if e.blocks_live]


def enable_confirmation_phrase(limits: HardLimits) -> str:
    return f"ENABLE LIVE {limits.policy_version}"


def enable_live(
    store: AuditStore,
    limits: HardLimits,
    *,
    operator: str,
    confirmation: str,
    limitations_path: Path,
) -> LiveState:
    """Run the ceremony; raises LiveEnableRefused and changes nothing on failure."""
    if not operator.strip():
        raise LiveEnableRefused("an identified operator is required")
    try:
        validate_limits(limits)
    except ValueError as error:
        raise LiveEnableRefused(f"hard limits rejected: {error}") from error
    if confirmation != enable_confirmation_phrase(limits):
        raise LiveEnableRefused("confirmation phrase does not match the shown limits")
    if store.fail_safe_paused():
        raise LiveEnableRefused("kill switch or execution pause is active")
    blockers = blocking_limitations(limitations_path)
    if blockers:
        raise LiveEnableRefused(
            "LIVE blocked by known limitations: " + "; ".join(blockers)
        )
    store.record_structured_audit(
        KIND_ENABLED,
        arguments=limits.to_payload(),
        client_id=operator.strip(),
        tool_name="live-enable",
        capability_scope=_SCOPE,
        policy_version=limits.policy_version,
        outcome="ENABLED",
    )
    return live_state(store)


def revoke_live(store: AuditStore, *, operator: str) -> LiveState:
    """Return to PAPER; always allowed, always recorded."""
    if not operator.strip():
        raise LiveEnableRefused("an identified operator is required")
    store.record_structured_audit(
        KIND_REVOKED,
        client_id=operator.strip(),
        tool_name="live-revoke",
        capability_scope=_SCOPE,
        outcome="REVOKED",
    )
    return live_state(store)


# --- V1 section 18 checklist ---

UNEVIDENCED = "unevidenced"
EVIDENCED = "evidenced"
_MANUAL = "no automated evidence; needs CI, platform or reviewer evidence"
_MARKETING = re.compile(
    r"guaranteed profit|risk[- ]free|unhackable|100% secure|bug-free|zero bugs",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ChecklistItem:
    item_id: str
    section: str
    text: str
    status: str
    evidence: str


@dataclass(frozen=True, slots=True)
class Context:
    root: Path
    gates: dict[str, Any] | None = None
    dist: Path | None = None
    head_sha: str | None = None


_Check = Callable[[Context], tuple[bool, str] | None]


def _gate_passed(ctx: Context, *names: str) -> tuple[bool, str]:
    if ctx.gates is None:
        return False, "no gates report supplied"
    if not ctx.head_sha or ctx.gates.get("git_sha") != ctx.head_sha:
        return False, "gates report does not match the current commit"
    by_name = {g.get("name"): g for g in ctx.gates.get("gates", [])}
    failed = [n for n in names if by_name.get(n, {}).get("exit_code") != 0]
    if failed:
        return False, f"gate(s) missing or failing: {', '.join(failed)}"
    return True, "gates passed at " + ctx.head_sha[:12] + ": " + ", ".join(names)


def _files_exist(ctx: Context, *names: str) -> tuple[bool, str]:
    missing = [n for n in names if not (ctx.root / n).is_file()]
    if missing:
        return False, "missing: " + ", ".join(missing)
    return True, "present: " + ", ".join(names)


def _chk_repo_docs(ctx: Context) -> tuple[bool, str]:
    return _files_exist(
        ctx, "README.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md",
        "CHANGELOG.md", "docs/ARCHITECTURE.md", "docs/THREAT_MODEL.md",
    )  # fmt: skip


def _chk_api_docs(ctx: Context) -> tuple[bool, str]:
    present, detail = _files_exist(ctx, "docs/API.md", "docs/MCP.md")
    if not present:
        return False, detail
    passed, why = _gate_passed(ctx, "pytest")
    return passed, f"{detail}; {why}"


def _chk_default_paper(ctx: Context) -> tuple[bool, str]:
    policy_paper = RiskPolicy().live_mode_enabled is False
    return policy_paper, "RiskPolicy default live_mode_enabled is False" + (
        "" if policy_paper else " (VIOLATED)"
    )


def _chk_tests(ctx: Context) -> tuple[bool, str]:
    return _gate_passed(ctx, "pytest")


def _chk_static(ctx: Context) -> tuple[bool, str]:
    return _gate_passed(
        ctx, "ruff-check", "ruff-format", "mypy", "secret-scan",
        "dependency-audit", "sast",
    )  # fmt: skip


def _chk_security_file(ctx: Context) -> tuple[bool, str]:
    return _files_exist(ctx, "SECURITY.md")


def _chk_no_unresolved(ctx: Context) -> tuple[bool, str]:
    blockers = blocking_limitations(ctx.root / "docs" / "KNOWN_LIMITATIONS.md")
    if blockers:
        return False, "; ".join(blockers)
    return True, "register parses and lists no untested security-critical branch"


_RELEASE_REPOSITORY = "timfromhcs/hcs-trading-intelligence"
_PROVENANCE_BUNDLE = "build-provenance.sigstore.json"


def _verified_release_files(directory: Path) -> set[str]:
    sums = directory / "SHA256SUMS"
    listed: set[str] = set()
    for line in sums.read_text(encoding="ascii").splitlines():
        digest, _, name = line.partition("  ")
        if (
            not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*", name)
            or name in listed
        ):
            raise ValueError("Invalid or duplicate SHA256SUMS entry")
        target = directory / name
        if target.is_symlink() or not target.is_file():
            raise ValueError(f"Missing or invalid release file: {name}")
        with target.open("rb") as file:
            if file_digest(file, "sha256").hexdigest() != digest:
                raise ValueError(f"Checksum mismatch: {name}")
        listed.add(name)
    expected = {
        path.name
        for path in directory.iterdir()
        if path.is_file()
        and path.name != sums.name
        and not path.name.endswith(".sha256")
    }
    if not listed or listed != expected:
        raise ValueError("SHA256SUMS must cover every release file")
    return listed


def _chk_checksums(ctx: Context) -> tuple[bool, str]:
    if ctx.dist is None or not ctx.head_sha:
        return False, "release directory and source commit are required"
    files = _verified_release_files(ctx.dist)
    if not {"sbom.cdx.json", _PROVENANCE_BUNDLE} <= files:
        return False, "checksums verify; SBOM or provenance bundle is missing"
    sbom = json.loads((ctx.dist / "sbom.cdx.json").read_text(encoding="utf-8"))
    if (
        not isinstance(sbom, dict)
        or sbom.get("bomFormat") != "CycloneDX"
        or not isinstance(sbom.get("components"), list)
        or not sbom["components"]
    ):
        return False, "expected a CycloneDX SBOM with components"
    gh = shutil.which("gh")
    if gh is None:
        return False, "GitHub CLI provenance verification unavailable"
    for name in sorted(files - {_PROVENANCE_BUNDLE}):
        try:
            result = subprocess.run(  # noqa: S603
                [
                    gh,
                    "attestation",
                    "verify",
                    str((ctx.dist / name).resolve()),
                    "--bundle",
                    str((ctx.dist / _PROVENANCE_BUNDLE).resolve()),
                    "--repo",
                    _RELEASE_REPOSITORY,
                    "--signer-workflow",
                    f"{_RELEASE_REPOSITORY}/.github/workflows/release.yml",
                    "--source-digest",
                    ctx.head_sha,
                ],
                capture_output=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False, "GitHub CLI provenance verification unavailable"
        if result.returncode != 0:
            return False, f"Provenance verification failed: {name}"
    return (
        True,
        "complete checksums, CycloneDX inventory and verified release provenance",
    )


def _chk_build(ctx: Context, platform: str, *patterns: str) -> tuple[bool, str]:
    if ctx.dist is None:
        return False, "no release directory supplied"
    evidence = json.loads(
        (ctx.dist / "build-evidence.json").read_text(encoding="utf-8")
    )
    if not isinstance(evidence, dict) or evidence.get("commit") != ctx.head_sha:
        return False, "build evidence does not match the source commit"
    builds = evidence.get("artifacts")
    files = builds.get(platform) if isinstance(builds, dict) else None
    if not isinstance(files, dict) or len(files) != len(patterns):
        return False, f"incomplete {platform} build evidence"
    for pattern in patterns:
        names = [name for name in files if fnmatchcase(name, pattern)]
        if len(names) != 1:
            return (
                False,
                f"expected one attested {platform} artifact matching {pattern}",
            )
        name = names[0]
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*", name):
            return False, "invalid build artifact name"
        with (ctx.dist / name).open("rb") as file:
            if file_digest(file, "sha256").hexdigest() != files[name]:
                return False, f"build-evidence checksum mismatch: {name}"
    return True, f"attested {platform} build and smoke-tested artifacts"


def _chk_windows(ctx: Context) -> tuple[bool, str]:
    return _chk_build(ctx, "windows", "HCS-Trading-Intelligence.exe")


def _chk_macos(ctx: Context) -> tuple[bool, str]:
    return _chk_build(ctx, "macos", "HCS-Trading-Intelligence-darwin-*.tar.gz")


def _chk_linux(ctx: Context) -> tuple[bool, str]:
    return _chk_build(ctx, "linux", "HCS-Trading-Intelligence-linux-*.tar.gz")


def _chk_wheels(ctx: Context) -> tuple[bool, str]:
    return _chk_build(ctx, "python", "hcs_trader-*.whl", "hcs_trader-*.tar.gz")


def _chk_changelog(ctx: Context) -> tuple[bool, str]:
    return _files_exist(ctx, "CHANGELOG.md", "docs/THIRD_PARTY_LICENSES.md")


_ITEMS: tuple[tuple[str, str, str, _Check | None], ...] = (
    ("P-01", "Product", "Public repository has README, license, security policy, contribution guide, architecture, threat model and changelog.", _chk_repo_docs),
    ("P-02", "Product", "Application runs locally on supported Windows, macOS and Linux targets.", None),
    ("P-03", "Product", "UI is polished, responsive, keyboard accessible and clearly branded HCS.media.", None),
    ("P-04", "Product", "Local REST API and MCP work with documented schemas, capabilities and permissions.", _chk_api_docs),
    ("P-05", "Product", "Local model is optional; application works without it or with internet disabled.", None),
    ("P-06", "Product", "Live data/news source attribution, timestamps, quotas and licensing notes are visible.", None),
    ("P-07", "Product", "Asset and network capability matrix is honest and tested.", None),
    ("Q-01", "Quant", "RSI and core indicator tests pass on edge cases and cross-checks.", _chk_tests),
    ("Q-02", "Quant", "Backtesting includes fees/slippage and prevents look-ahead/leakage.", _chk_tests),
    ("Q-03", "Quant", "Strategy candidate promotion uses walk-forward and paper-shadow gates.", _chk_tests),
    ("Q-04", "Quant", "LLM cannot change strategy/risk policy or call signer directly.", None),
    ("Q-05", "Quant", "News/web content is treated as untrusted; prompt-injection suite passes.", _chk_tests),
    ("E-01", "Portfolio / execution", "Portfolio reconciliation and event idempotency pass.", _chk_tests),
    ("E-02", "Portfolio / execution", "Wallet read-only capability works for every claimed chain.", None),
    ("E-03", "Portfolio / execution", "Every enabled live execution route passes testnet/devnet/simulation and transaction-decoder tests.", None),
    ("E-04", "Portfolio / execution", "Default mode is PAPER; LIVE requires explicit local opt-in.", _chk_default_paper),
    ("E-05", "Portfolio / execution", "Cold reserve keys are not accessible to the application.", None),
    ("E-06", "Portfolio / execution", "Hot-wallet exposure and risk limits are configured, visible, persisted and enforced.", None),
    ("E-07", "Portfolio / execution", "No arbitrary withdrawal, arbitrary contract call, unbounded allowance or unlimited transfer capability is exposed to agents.", None),
    ("E-08", "Portfolio / execution", "Kill switch persists, blocks execution and has a tested recovery path.", None),
    ("E-09", "Portfolio / execution", "Ambiguous transactions/orders are reconciled before retries.", None),
    ("R-01", "Reliability / security", "Unit, integration, property, regression, chaos and packaging tests pass.", _chk_tests),
    ("R-02", "Reliability / security", "Static analysis, dependency audit, secret scan, SAST and license checks pass with reviewed findings.", _chk_static),
    ("R-03", "Reliability / security", "Logs are structured and secret-redacted.", None),
    ("R-04", "Reliability / security", "Fresh installs and upgrades pass OS-specific smoke tests.", None),
    ("R-05", "Reliability / security", "No known critical/high unresolved security issue in signer, wallet, risk, MCP or update paths.", _chk_no_unresolved),
    ("R-06", "Reliability / security", "SECURITY.md explains reporting and response process.", _chk_security_file),
    ("D-01", "Release engineering", "Windows executable built on Windows runner.", _chk_windows),
    ("D-02", "Release engineering", "macOS artifact built on macOS runner.", _chk_macos),
    ("D-03", "Release engineering", "Linux artifact built on Linux runner.", _chk_linux),
    ("D-04", "Release engineering", "Wheels/sdist built with cibuildwheel where required and installed in clean environments.", _chk_wheels),
    ("D-05", "Release engineering", "SHA-256 checksums, SBOM and artifact provenance attached.", _chk_checksums),
    ("D-06", "Release engineering", "README feature claims match tested implementation.", None),
    ("D-07", "Release engineering", "Changelog and third-party license attributions are complete.", _chk_changelog),
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class ChecklistReport:
    items: list[ChecklistItem]
    claims_findings: list[str] = field(default_factory=list)
    live_blockers: list[str] = field(default_factory=list)

    @property
    def unmet(self) -> list[ChecklistItem]:
        return [i for i in self.items if i.status != EVIDENCED]

    @property
    def v1_allowed(self) -> bool:
        return not self.unmet and not self.claims_findings and not self.live_blockers

    def to_json(self) -> dict[str, Any]:
        return {
            "v1_allowed": self.v1_allowed,
            "evidenced": len(self.items) - len(self.unmet),
            "unevidenced": len(self.unmet),
            "items": [asdict(i) for i in self.items],
            "claims_findings": self.claims_findings,
            "live_blockers": self.live_blockers,
        }


def scan_claims(root: Path) -> list[str]:
    """Flag forbidden marketing claims in the README (negations are not exempt)."""
    readme = root / "README.md"
    if not readme.is_file():
        return ["README.md is missing"]
    text = readme.read_text(encoding="utf-8")
    return [
        f"README.md contains forbidden claim '{m.group(0)}'"
        for m in _MARKETING.finditer(text)
    ]


def capability_findings() -> list[str]:
    """Signing capabilities that are not chain-valid must say so in their note."""
    return [
        f"signing namespace '{ns}' is not chain-valid but its note does not say 'not'"
        for ns, cap in signing_capabilities().items()
        if not cap.chain_valid and "not" not in cap.note.lower()
    ]


def evaluate_checklist(ctx: Context) -> ChecklistReport:
    """Evaluate every section 18 checkbox; a failing or crashing check is unevidenced."""
    try:
        provenance = _chk_checksums(ctx)
    except (OSError, ValueError) as error:
        provenance = False, f"check failed: {error}"
    items: list[ChecklistItem] = []
    for item_id, section, text, check in _ITEMS:
        status, evidence = UNEVIDENCED, _MANUAL
        if item_id == "D-05":
            ok, evidence = provenance
            status = EVIDENCED if ok else UNEVIDENCED
        elif item_id in {"D-01", "D-02", "D-03", "D-04"} and not provenance[0]:
            evidence = "release artifact provenance must be verified first"
        elif check is not None:
            try:
                ok, detail = check(ctx) or (False, _MANUAL)
            except (OSError, ValueError) as error:
                ok, detail = False, f"check failed: {error}"
            status, evidence = (EVIDENCED if ok else UNEVIDENCED), detail
        items.append(ChecklistItem(item_id, section, text, status, evidence))
    return ChecklistReport(
        items,
        scan_claims(ctx.root) + capability_findings(),
        blocking_limitations(ctx.root / "docs" / "KNOWN_LIMITATIONS.md"),
    )


def git_head(root: Path) -> str | None:
    """Current commit SHA of the checkout, or None when it cannot be determined."""
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(root), "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = result.stdout.strip()
    return (
        sha if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else None
    )


def load_context(
    root: Path, gates_report: Path | None = None, dist: Path | None = None
) -> Context:
    gates: dict[str, Any] | None = None
    if gates_report is not None:
        loaded = json.loads(gates_report.read_text(encoding="utf-8"))
        gates = loaded if isinstance(loaded, dict) else None
    return Context(root=root, gates=gates, dist=dist, head_sha=git_head(root))
