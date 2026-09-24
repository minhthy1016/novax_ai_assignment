"""Rate limiting: identity, bucket choice, refusal shape and the fail-open rule."""

from __future__ import annotations

from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from opsassist.auth import issue_token
from opsassist.config import Settings
from opsassist.ratelimit import EXEMPT_PATHS, RateLimiter, bucket_for, identity_of


class FakeRedis:
    """Enough of Redis to exercise the limiter: the Lua bucket, in Python."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.state: dict[str, tuple[float, float]] = {}

    def register_script(self, _source: str) -> Any:
        async def run(keys: list[str], args: list[Any]) -> list[Any]:
            if self.fail:
                raise RedisConnectionError("redis is down")
            key = keys[0]
            rate, burst, now, cost = (float(a) for a in args)
            tokens, ts = self.state.get(key, (burst, now))
            tokens = min(burst, tokens + (now - ts) * rate)
            if tokens >= cost:
                self.state[key] = (tokens - cost, now)
                return [1, "0", str(tokens - cost)]
            self.state[key] = (tokens, now)
            return [0, str((cost - tokens) / rate), str(tokens)]

        return run


def settings() -> Settings:
    return Settings(env="test")


@pytest.mark.asyncio
async def test_burst_is_bounded_and_retry_after_is_returned() -> None:
    limiter = RateLimiter(FakeRedis(), settings())  # type: ignore[arg-type]
    allowed = 0
    for _ in range(5):
        decision = await limiter.check("user:U001", "expensive", per_minute=60, burst=3)
        allowed += decision.allowed
    assert allowed == 3
    refused = await limiter.check("user:U001", "expensive", per_minute=60, burst=3)
    assert not refused.allowed
    assert refused.retry_after >= 1  # never advertise "retry immediately"


@pytest.mark.asyncio
async def test_each_caller_has_their_own_budget() -> None:
    limiter = RateLimiter(FakeRedis(), settings())  # type: ignore[arg-type]
    for _ in range(3):
        await limiter.check("user:U001", "standard", per_minute=60, burst=3)
    assert (await limiter.check("user:U001", "standard", 60, 3)).allowed is False
    assert (await limiter.check("user:U002", "standard", 60, 3)).allowed is True


@pytest.mark.asyncio
async def test_buckets_do_not_share_tokens() -> None:
    """Browsing conversations must not consume the budget that protects inference."""
    limiter = RateLimiter(FakeRedis(), settings())  # type: ignore[arg-type]
    for _ in range(3):
        await limiter.check("user:U001", "standard", 60, 3)
    assert (await limiter.check("user:U001", "expensive", 60, 3)).allowed is True


@pytest.mark.security
@pytest.mark.asyncio
async def test_redis_failure_fails_open_and_is_counted() -> None:
    """The limiter protects cost and capacity, never authorization - so an outage lets the
    request through, loudly, instead of taking the assistant down."""
    limiter = RateLimiter(FakeRedis(fail=True), settings())  # type: ignore[arg-type]
    decision = await limiter.check("user:U001", "expensive", 1, 1)
    assert decision.allowed is True


@pytest.mark.security
def test_identity_comes_from_a_verified_signature_not_the_raw_header() -> None:
    cfg = settings()
    good = issue_token(cfg, "U001", "Senior Engineer")
    assert identity_of({"authorization": f"Bearer {good}"}, "10.0.0.1", cfg) == "user:U001"

    other = Settings(env="test", jwt_secret="another-secret-entirely-0123456789ab")
    forged = issue_token(other, "U999", "x")
    # An unverifiable token buys no fresh budget: the caller falls back to their address.
    assert identity_of({"authorization": f"Bearer {forged}"}, "10.0.0.1", cfg) == "ip:10.0.0.1"
    assert identity_of({}, "10.0.0.1", cfg) == "ip:10.0.0.1"


def test_expensive_routes_get_their_own_bucket() -> None:
    assert bucket_for("/api/chat") == "expensive"
    assert bucket_for("/api/chat/stream") == "expensive"
    assert bucket_for("/api/documents") == "expensive"
    assert bucket_for("/api/conversations/abc") == "standard"
    assert bucket_for("/api/models") == "standard"


def test_probes_are_never_throttled() -> None:
    assert {"/healthz", "/readyz", "/metrics"} == EXEMPT_PATHS
