"""Bounded retry with jittered backoff and a circuit breaker (injectable time)."""

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitOpen(Exception):
    """The breaker is open; the provider must not be called yet."""

    def __init__(self, retry_in: float) -> None:
        super().__init__("Circuit breaker is open")
        self.retry_in = retry_in


class RetryBudgetExhausted(Exception):
    """All permitted attempts or the total delay budget were used."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 30.0
    total_budget: float = 60.0
    jitter: float = 0.5  # fraction of the delay randomised downwards

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.base_delay < 0 or self.max_delay < 0:
            raise ValueError("Invalid retry policy")
        if not 0 <= self.jitter <= 1 or self.total_budget < 0:
            raise ValueError("Invalid retry policy")

    def delay(
        self, attempt: int, rng: random.Random, retry_after: float | None
    ) -> float:
        """Delay before retry number ``attempt`` (1-based), jittered and bounded."""
        raw = float(min(self.max_delay, self.base_delay * 2 ** (attempt - 1)))
        value = raw * (1 - self.jitter * rng.random())
        if retry_after is not None and retry_after > 0:
            value = max(value, min(retry_after, self.max_delay))
        return value


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if failure_threshold < 1 or reset_timeout < 0:
            raise ValueError("Invalid circuit breaker configuration")
        self._threshold = failure_threshold
        self._reset = reset_timeout
        self._clock = clock
        self._failures = 0
        self._opened_at = 0.0
        self._state = CircuitState.CLOSED
        self._probe_in_flight = False

    @property
    def state(self) -> CircuitState:
        if (
            self._state is CircuitState.OPEN
            and self._clock() - self._opened_at >= self._reset
        ):
            return CircuitState.HALF_OPEN
        return self._state

    def before_call(self) -> None:
        state = self.state
        if state is CircuitState.OPEN:
            raise CircuitOpen(self._reset - (self._clock() - self._opened_at))
        if state is CircuitState.HALF_OPEN:
            if self._probe_in_flight:
                raise CircuitOpen(0.0)
            self._state = CircuitState.HALF_OPEN
            self._probe_in_flight = True

    def record_success(self) -> None:
        self._failures = 0
        self._probe_in_flight = False
        self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self._probe_in_flight = False
        self._failures += 1
        if self._state is CircuitState.HALF_OPEN or self._failures >= self._threshold:
            self._state = CircuitState.OPEN
            self._opened_at = self._clock()


async def call_with_retry[T](
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy = RetryPolicy(),
    breaker: CircuitBreaker | None = None,
    retryable: tuple[type[BaseException], ...] = (Exception,),
    non_retryable: tuple[type[BaseException], ...] = (),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: random.Random | None = None,
) -> T:
    """Run ``operation`` under the retry budget with one breaker check per attempt.

    ``CircuitOpen`` propagates without calling the provider. Exceptions carrying a
    ``retry_after`` attribute (rate limits) lengthen the delay.
    """
    generator = rng or random.Random()  # noqa: S311 - jitter, not security
    spent = 0.0
    last: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        if breaker is not None:
            breaker.before_call()
        try:
            result = await operation()
        except non_retryable:
            if breaker is not None:
                breaker.record_failure()
            raise
        except retryable as error:
            last = error
            if breaker is not None:
                breaker.record_failure()
            if attempt == policy.max_attempts:
                break
            delay = policy.delay(
                attempt, generator, getattr(error, "retry_after", None)
            )
            if spent + delay > policy.total_budget:
                break
            spent += delay
            await sleep(delay)
        else:
            if breaker is not None:
                breaker.record_success()
            return result
    raise RetryBudgetExhausted("Retry budget exhausted") from last
