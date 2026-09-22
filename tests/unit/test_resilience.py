import random

from opsassist.gateway.resilience import BackoffPolicy, BreakerState, CircuitBreaker
from opsassist.providers.base import ProviderRateLimited, ProviderUnavailable


def test_backoff_is_jittered_and_capped() -> None:
    policy = BackoffPolicy(base_s=0.5, cap_s=4.0)
    rng = random.Random(7)
    err = ProviderUnavailable("p", "x")
    for attempt in range(1, 10):
        delay = policy.delay(attempt, err, rng)
        assert 0 <= delay <= min(4.0, 0.5 * 2 ** (attempt - 1))


def test_retry_after_is_honoured_up_to_cap() -> None:
    policy = BackoffPolicy(cap_s=4.0)
    assert policy.delay(1, ProviderRateLimited("p", "x", retry_after_s=1.5)) == 1.5
    assert policy.delay(1, ProviderRateLimited("p", "x", retry_after_s=60)) == 4.0


def test_breaker_opens_half_opens_and_closes() -> None:
    now = [0.0]
    breaker = CircuitBreaker(failure_threshold=2, cooldown_s=10, clock=lambda: now[0])
    breaker.record_failure()
    assert breaker.state is BreakerState.CLOSED
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN and not breaker.allow()

    now[0] = 10.0
    assert breaker.state is BreakerState.HALF_OPEN
    assert breaker.allow()  # one probe
    assert not breaker.allow()  # ...and only one
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED


def test_failed_probe_reopens_breaker() -> None:
    now = [0.0]
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=5, clock=lambda: now[0])
    breaker.record_failure()
    now[0] = 5.0
    assert breaker.allow()
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
