"""The default network block in tests/conftest.py fails closed and is opt-in only."""

import socket

import pytest


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
