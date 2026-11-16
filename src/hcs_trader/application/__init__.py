"""Transport-independent use cases shared by the REST API and the MCP server."""

from hcs_trader.application.capabilities import (
    ChainCapability,
    chain_capability_matrix,
)
from hcs_trader.application.service import (
    ApplicationError,
    ApplicationService,
    Forbidden,
    InvalidRequest,
    NotFound,
    Rejected,
    Unavailable,
)

__all__ = [
    "ApplicationError",
    "ApplicationService",
    "ChainCapability",
    "Forbidden",
    "InvalidRequest",
    "NotFound",
    "Rejected",
    "Unavailable",
    "chain_capability_matrix",
]
