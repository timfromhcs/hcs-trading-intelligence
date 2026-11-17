"""System mode and per-mode outbound network allowlist (spec 0.6).

The policy is enforced by a request event hook installed through
``build_http_client``; callers inject it where they create HTTP clients.
"""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlsplit

import httpx2


class SystemMode(StrEnum):
    OFFLINE = "OFFLINE"
    DATA_ONLY = "DATA_ONLY"
    PAPER = "PAPER"
    LIVE = "LIVE"


class NetworkBlocked(Exception):
    """Raised before any bytes leave the process."""


PUBLIC_DATA_HOSTS = frozenset(
    {
        "api.kraken.com",
        "api.stlouisfed.org",
        "data-api.ecb.europa.eu",
        "www.federalreserve.gov",
    }
)
_KRAKEN_PRIVATE_PREFIX = "/0/private/"
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class NetworkPolicy:
    def __init__(
        self,
        mode: SystemMode = SystemMode.PAPER,
        *,
        extra_data_hosts: frozenset[str] = frozenset(),
        live_hosts: frozenset[str] = frozenset(),
    ) -> None:
        self.mode = mode
        self._data_hosts = PUBLIC_DATA_HOSTS | extra_data_hosts
        self._live_hosts = live_hosts

    def check(self, url: str) -> None:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" and host not in _LOCAL_HOSTS:
            raise NetworkBlocked("only https outbound requests are permitted")
        if host in _LOCAL_HOSTS:
            return  # loopback (local node/model) never leaves the machine
        if self.mode is SystemMode.OFFLINE:
            raise NetworkBlocked("OFFLINE mode permits no outbound network access")
        if self.mode is SystemMode.LIVE and host in self._live_hosts:
            return
        if host not in self._data_hosts:
            raise NetworkBlocked(f"host {host!r} is not allowlisted for {self.mode}")
        if (
            host == "api.kraken.com"
            and parts.path.startswith(_KRAKEN_PRIVATE_PREFIX)
            and self.mode is not SystemMode.LIVE
        ):
            raise NetworkBlocked("private venue endpoints require LIVE mode")


def build_http_client(policy: NetworkPolicy, **kwargs: object) -> httpx2.AsyncClient:
    """Create an AsyncClient that refuses requests the policy does not allow."""

    async def _guard(request: httpx2.Request) -> None:
        policy.check(str(request.url))

    hooks = dict(kwargs.pop("event_hooks", {}) or {})  # type: ignore[call-overload]
    hooks["request"] = [_guard, *hooks.get("request", [])]
    kwargs.setdefault("trust_env", False)
    return httpx2.AsyncClient(event_hooks=hooks, **kwargs)  # type: ignore[arg-type]
