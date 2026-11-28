"""Host and Origin validation shared by the local REST API and the MCP HTTP path.

A loopback bind alone does not stop DNS rebinding: a page on an attacker domain
that resolves to 127.0.0.1 reaches the socket with its own Host header. Both
transports therefore accept only an explicit set of host names and, when the
bound port is known, only that port.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

DEFAULT_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})


class HostPolicy:
    """Allowlist of local host names, optionally pinned to the bound port."""

    def __init__(
        self, port: int | None = None, extra_hosts: Iterable[str] = ()
    ) -> None:
        self.port = port
        self.hosts = DEFAULT_HOSTS | {h.strip().lower() for h in extra_hosts if h}

    def _authority_ok(self, authority: str, *, default_port: int | None) -> bool:
        if not authority or any(c in authority for c in "@/\\ ?#"):
            return False
        try:
            parts = urlsplit(f"//{authority}")
            hostname = parts.hostname
            port = parts.port
        except ValueError:
            return False
        if hostname is None or hostname not in self.hosts:
            return False
        if self.port is None:
            return True
        return (port if port is not None else default_port) == self.port

    def host_allowed(self, host_header: str | None) -> bool:
        """Validate a Host header value. An absent Host is rejected."""
        if host_header is None:
            return False
        return self._authority_ok(host_header.strip(), default_port=80)

    def origin_allowed(self, origin: str) -> bool:
        """Validate an Origin header value; only http origins on local hosts."""
        try:
            parts = urlsplit(origin)
        except ValueError:
            return False
        if parts.scheme != "http" or parts.path not in ("", "/") or parts.query:
            return False
        return self._authority_ok(parts.netloc, default_port=80)


def fetch_site_allowed(value: str | None) -> bool:
    """Sec-Fetch-Site must be absent, same-origin or none for state changes."""
    return value is None or value.strip().lower() in ALLOWED_FETCH_SITES
