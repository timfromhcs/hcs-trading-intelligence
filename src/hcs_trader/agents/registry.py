"""Declared agent roles and permissions (AGENTS 8.1).

Permissions are enforced at the call boundary by `authorize`; a role description
or model output never grants anything. No role may reach the signer secret,
withdraw funds, clear the kill switch or change risk policy.
"""

from dataclasses import dataclass
from enum import StrEnum


class AgentRole(StrEnum):
    MARKET_DATA = "market_data"
    NEWS = "news"
    QUANT_RESEARCH = "quant_research"
    PORTFOLIO = "portfolio"
    RISK_AUDITOR = "risk_auditor"
    EXECUTION = "execution"
    LEARNING = "learning"
    OPERATIONS = "operations"


FORBIDDEN_TOOLS = frozenset(
    {
        "signer.sign_arbitrary",
        "signer.read_secret",
        "wallet.withdraw",
        "wallet.export_seed",
        "risk.mutate_policy",
        "system.clear_kill_switch",
        "strategy.promote_self",
        "shell.run",
    }
)


@dataclass(frozen=True, slots=True)
class AgentSpec:
    role: AgentRole
    allowed_tools: frozenset[str]
    description: str

    def __post_init__(self) -> None:
        if self.allowed_tools & FORBIDDEN_TOOLS:
            raise ValueError(f"{self.role} declares a forbidden tool")


_SPECS = (
    AgentSpec(
        AgentRole.MARKET_DATA,
        frozenset({"provider.read", "market_store.write"}),
        "Collect, normalize and validate prices and candles.",
    ),
    AgentSpec(
        AgentRole.NEWS,
        frozenset({"public_feed.read", "news_store.write"}),
        "Ingest and classify news; article text is untrusted data.",
    ),
    AgentSpec(
        AgentRole.QUANT_RESEARCH,
        frozenset({"market_store.read", "indicators.calculate", "candidate.create"}),
        "Compute indicators and create sandbox strategy candidates.",
    ),
    AgentSpec(
        AgentRole.PORTFOLIO,
        frozenset({"wallet.read_balances", "portfolio.snapshot"}),
        "Reconcile balances and exposure.",
    ),
    AgentSpec(
        AgentRole.RISK_AUDITOR,
        frozenset({"risk.read_policy", "risk.read_decisions"}),
        "Explain deterministic risk decisions.",
    ),
    AgentSpec(
        AgentRole.EXECUTION,
        frozenset({"execution.submit_validated"}),
        "Submit only already-approved, typed plans.",
    ),
    AgentSpec(
        AgentRole.LEARNING,
        frozenset({"trades.read", "candidate.create"}),
        "Evaluate outcomes and propose candidates within a fixed quota.",
    ),
    AgentSpec(
        AgentRole.OPERATIONS,
        frozenset({"health.read", "system.pause"}),
        "Watch health and pause; never resume or sign.",
    ),
)

REGISTRY: dict[AgentRole, AgentSpec] = {spec.role: spec for spec in _SPECS}


def authorize(role: AgentRole, tool: str) -> bool:
    """Deny by default: unknown roles, unknown tools and forbidden tools fail."""
    spec = REGISTRY.get(role)
    return (
        spec is not None and tool in spec.allowed_tools and tool not in FORBIDDEN_TOOLS
    )


@dataclass(frozen=True, slots=True)
class CandidateQuota:
    """Fixed compute budget for the Learning agent (AGENTS 8.4)."""

    max_candidates_per_day: int = 3

    def allows(self, created_today: int) -> bool:
        return 0 <= created_today < self.max_candidates_per_day
