"""Shared pytest configuration: Hypothesis profiles and a default network block.

Hypothesis profiles, selected by ``HYPOTHESIS_PROFILE`` (``ci`` when ``CI`` is set,
otherwise ``dev``):

* ``dev``: random, example database on, modest example count.
* ``ci``: derandomised, no example database, no deadline, fixed example count, so a CI
  failure reproduces exactly.
* ``nightly``: many more examples, random seeds, no deadline.

Tests that set their own ``@settings(max_examples=...)`` keep that explicit value; the
profile supplies every setting they do not override.

Network block: tests must not touch real remote hosts (AGENTS.md section 13). Socket
connections to non-loopback addresses and DNS lookups for non-local names raise
``NetworkBlockedError`` unless the test carries ``@pytest.mark.allow_network`` or one of
the ``rehearsal`` / ``regtest`` markers. Loopback (local fake servers, the in-process
API) and Unix sockets stay open. Child processes are not affected.
"""

import ipaddress
import os
import socket
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, settings

# Packaging tests import release scripts directly (was tests/packaging/conftest.py).
_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

settings.register_profile("dev", max_examples=100)
settings.register_profile(
    "ci",
    max_examples=100,
    derandomize=True,
    database=None,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "nightly",
    max_examples=500,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile(
    os.environ.get("HYPOTHESIS_PROFILE") or ("ci" if os.environ.get("CI") else "dev")
)

NETWORK_MARKERS = ("allow_network", "rehearsal", "regtest")
_LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", ""}


class NetworkBlockedError(BaseException):  # noqa: N818, B903
    """Raised when a test tries to reach a non-loopback host without opting in.

    Deliberately a BaseException (like KeyboardInterrupt) so a broad
    ``except Exception`` in code under test cannot swallow the block.
    """


def _is_loopback_host(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host is None:
        return True
    text = str(host).strip("[]")
    if text.lower() in _LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(text.split("%")[0]).is_loopback
    except ValueError:
        return False


def _is_loopback_address(address: object) -> bool:
    if isinstance(address, str | bytes):  # AF_UNIX path
        return True
    if isinstance(address, tuple) and address:
        return _is_loopback_host(address[0])
    return False


@pytest.fixture(autouse=True)
def _block_real_network(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    if any(request.node.get_closest_marker(name) for name in NETWORK_MARKERS):
        yield
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self: socket.socket, address: Any) -> None:
        if not _is_loopback_address(address):
            raise NetworkBlockedError(f"blocked connect to {address!r}")
        real_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> int:
        if not _is_loopback_address(address):
            raise NetworkBlockedError(f"blocked connect_ex to {address!r}")
        return real_connect_ex(self, address)

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not _is_loopback_host(host):
            try:
                ipaddress.ip_address(str(host).strip("[]").split("%")[0])
            except ValueError:
                raise NetworkBlockedError(f"blocked DNS lookup for {host!r}") from None
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    yield
