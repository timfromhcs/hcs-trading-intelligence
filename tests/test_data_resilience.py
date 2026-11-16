import random
from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from hcs_trader.data.ecb import MacroRateLimited, fetch_ecb_rates
from hcs_trader.data.fed_news import NewsRateLimited, fetch_fed_news
from hcs_trader.data.provider import parse_retry_after
from hcs_trader.data.resilience import (
    CircuitBreaker,
    CircuitOpen,
    CircuitState,
    RetryBudgetExhausted,
    RetryPolicy,
    call_with_retry,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class Boom(Exception):
    retry_after: float | None = None


@pytest.mark.anyio
async def test_retries_with_jitter_then_succeeds() -> None:
    clock = Clock()
    calls = 0

    async def op() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise Boom
        return "ok"

    result = await call_with_retry(
        op,
        policy=RetryPolicy(max_attempts=4, base_delay=1, jitter=0.5),
        sleep=clock.sleep,
        rng=random.Random(1),  # noqa: S311
    )
    assert result == "ok" and calls == 3
    assert 0.5 <= clock.sleeps[0] <= 1.0 and 1.0 <= clock.sleeps[1] <= 2.0


@pytest.mark.anyio
async def test_budget_exhausted_and_retry_after_honoured() -> None:
    clock = Clock()

    async def op() -> None:
        error = Boom()
        error.retry_after = 20
        raise error

    with pytest.raises(RetryBudgetExhausted):
        await call_with_retry(
            op,
            policy=RetryPolicy(max_attempts=5, base_delay=1, total_budget=30),
            sleep=clock.sleep,
            rng=random.Random(2),  # noqa: S311
        )
    assert clock.sleeps == [20]  # second 20s sleep would exceed the 30s budget


@pytest.mark.anyio
async def test_non_retryable_is_not_retried() -> None:
    clock = Clock()
    calls = 0

    async def op() -> None:
        nonlocal calls
        calls += 1
        raise KeyError("x")

    with pytest.raises(KeyError):
        await call_with_retry(op, non_retryable=(KeyError,), sleep=clock.sleep)
    assert calls == 1 and clock.sleeps == []


@pytest.mark.anyio
async def test_breaker_opens_half_opens_and_closes() -> None:
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=2, reset_timeout=10, clock=clock)
    calls = 0

    async def fail() -> None:
        nonlocal calls
        calls += 1
        raise Boom

    for _ in range(2):
        with pytest.raises(RetryBudgetExhausted):
            await call_with_retry(
                fail, policy=RetryPolicy(max_attempts=1), breaker=breaker
            )
    assert breaker.state.value == "OPEN"
    with pytest.raises(CircuitOpen):
        await call_with_retry(fail, breaker=breaker)
    assert calls == 2  # open breaker never reaches the provider

    clock.t = 10
    assert breaker.state is CircuitState.HALF_OPEN

    async def ok() -> int:
        return 1

    assert await call_with_retry(ok, breaker=breaker) == 1
    assert str(breaker.state) == "CLOSED"


def test_half_open_failure_reopens_and_single_probe() -> None:
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout=5, clock=clock)
    breaker.before_call()
    breaker.record_failure()
    clock.t = 5
    breaker.before_call()
    with pytest.raises(CircuitOpen):
        breaker.before_call()  # only one probe in flight
    breaker.record_failure()
    assert breaker.state.value == "OPEN"


def test_policy_validation() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=0)


def test_parse_retry_after() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert parse_retry_after("30") == 30
    assert parse_retry_after("999999") == 3600
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None
    http_date = (now + timedelta(seconds=90)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert parse_retry_after(http_date, now=now) == 90


@pytest.mark.anyio
async def test_fetchers_surface_rate_limit_with_retry_after() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, headers={"Retry-After": "42"})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        with pytest.raises(NewsRateLimited) as news:
            await fetch_fed_news(client)
        with pytest.raises(MacroRateLimited) as macro:
            await fetch_ecb_rates(client)
    assert news.value.retry_after == 42 and macro.value.retry_after == 42
