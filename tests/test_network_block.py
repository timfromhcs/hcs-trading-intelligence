"""The default network block in tests/conftest.py fails closed and is opt-in only."""

import asyncio
import socket

import httpx2
import pytest

from hcs_trader.network_policy import (
    NetworkBlocked,
    NetworkPolicy,
    SystemMode,
    build_http_client,
)


def test_non_loopback_connect_is_blocked() -> None:
    with socket.socket() as sock, pytest.raises(BaseException, match="blocked"):
        sock.connect(("203.0.113.7", 80))


def test_non_loopback_connect_ex_is_blocked() -> None:
    with socket.socket() as sock, pytest.raises(BaseException, match="blocked"):
        sock.connect_ex(("203.0.113.7", 80))


def test_dns_lookup_for_a_remote_name_is_blocked() -> None:
    with pytest.raises(BaseException, match="blocked"):
        socket.getaddrinfo("example.com", 443)


def test_loopback_server_still_works() -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.socket() as client:
            client.connect(server.getsockname())
            conn, _ = server.accept()
            conn.close()


@pytest.mark.allow_network
def test_opt_in_marker_lifts_the_block() -> None:
    # Only the guard is checked: a TEST-NET connect must not be blocked by it, though it
    # may still fail or time out for ordinary network reasons.
    with socket.socket() as sock:
        sock.settimeout(0.01)
        try:
            sock.connect(("203.0.113.7", 80))
        except OSError as error:
            assert "blocked" not in str(error)


def test_pinned_request_is_authorized_only_by_its_original_host() -> None:
    policy = NetworkPolicy(
        SystemMode.DATA_ONLY,
        extra_data_hosts=frozenset({"example.org"}),
    )
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200)

    async def exercise() -> None:
        async with build_http_client(
            policy, transport=httpx2.MockTransport(handler)
        ) as client:
            allowed = client.build_request(
                "GET",
                "https://93.184.216.34/news",
                extensions={"hcs_pinned_host": "example.org"},
            )
            await client.send(allowed)
            blocked = client.build_request("GET", "https://93.184.216.34/news")
            with pytest.raises(NetworkBlocked):
                await client.send(blocked)

    asyncio.run(exercise())
    assert len(seen) == 1
    assert seen[0].url.host == "93.184.216.34"


def test_block_is_not_swallowed_by_broad_except_exception() -> None:
    swallowed = False
    with pytest.raises(BaseException, match="blocked") as info:
        try:
            socket.getaddrinfo("example.com", 443)
        except Exception:  # noqa: BLE001  (what a lenient adapter might do)
            swallowed = True
    assert not swallowed
    assert type(info.value).__name__ == "NetworkBlockedError"
    assert not isinstance(info.value, Exception)
