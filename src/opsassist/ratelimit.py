"""Per-caller rate limiting: a token bucket in Redis, applied as ASGI middleware.

Why a token bucket and not a fixed window: a fixed window lets a caller spend the whole
budget in the last second of one window and again in the first second of the next. The
bucket refills continuously, so a burst is bounded by `burst` and the sustained rate by
`per_minute`, which is what protects the GPU queue behind `/api/chat`.

Why two budgets: a question costs a model call, a retrieval round trip and tokens; listing
conversations costs a SELECT. One budget for both is either too tight to browse or too loose
to protect inference, so expensive routes have their own, smaller bucket (D-61).

**This limiter protects capacity and cost, not authorization.** If Redis is unavailable it
fails *open* - a cache outage must not make an internal assistant unavailable - and the
event is logged and counted. Nothing about who may read what depends on it: that is the
access scope, row-level security and the tool permissions, none of which touch Redis.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import jwt
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.types import ASGIApp, Receive, Scope, Send

from opsassist.auth import decode_token
from opsassist.config import Settings
from opsassist.logging_setup import get_logger
from opsassist.metrics import RATE_LIMITED, RATE_LIMITER_UNAVAILABLE

log = get_logger("opsassist.ratelimit")

# Atomic refill-and-take. Returning the wait time lets the caller send Retry-After, and
# keeps the arithmetic in one place: the client never computes its own allowance.
_BUCKET_SCRIPT = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])      -- tokens per second
local burst = tonumber(ARGV[2])     -- bucket size
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local state = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil then
  tokens = burst
  ts = now
end
tokens = math.min(burst, tokens + (now - ts) * rate)
local allowed = 0
local retry = 0
if tokens >= cost then
  allowed = 1
  tokens = tokens - cost
else
  retry = (cost - tokens) / rate
end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, math.ceil(burst / rate) + 60)
return {allowed, tostring(retry), tostring(tokens)}
"""

# Probes and metrics are scraped on a schedule and must never be throttled; the token
# endpoint is limited by IP because there is no identity yet.
EXEMPT_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})
# Routes that cost a model call, an embedding or an ingestion job.
EXPENSIVE_PREFIXES = ("/api/chat", "/api/embeddings", "/api/search", "/api/documents")


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    retry_after: int
    remaining: int
    bucket: str


class RateLimiter:
    """Token buckets in Redis. One bucket per (identity, bucket name)."""

    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings
        self._script = redis.register_script(_BUCKET_SCRIPT)

    async def check(self, identity: str, bucket: str, per_minute: int, burst: int) -> Decision:
        rate = per_minute / 60.0
        key = f"ratelimit:{bucket}:{identity}"
        try:
            allowed, retry, tokens = await self._script(
                keys=[key], args=[rate, burst, time.time(), 1]
            )
        except RedisError as err:
            # Fail open: availability of the assistant outranks precision of a cost control.
            RATE_LIMITER_UNAVAILABLE.inc()
            log.warning("rate_limiter_unavailable", error=type(err).__name__)
            return Decision(True, 0, burst, bucket)
        return Decision(
            allowed=bool(int(allowed)),
            retry_after=max(1, math.ceil(float(retry))),
            remaining=max(0, int(float(tokens))),
            bucket=bucket,
        )


def identity_of(headers: dict[str, str], client: str, settings: Settings) -> str:
    """Who is being limited.

    The subject of a *validly signed* token, so a caller cannot get a fresh budget by
    editing the header; otherwise the client address, which is what an unauthenticated
    caller has. The signature is verified but the database is not consulted: this runs
    before routing, and an invalid token is rejected later by the route's own dependency.
    """
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        try:  # the same verification the routes use - one place decides a token is valid
            return f"user:{decode_token(settings, auth[7:].strip()).user_id}"
        except jwt.InvalidTokenError:
            return f"ip:{client}"
    return f"ip:{client}"


def bucket_for(path: str) -> str:
    return "expensive" if path.startswith(EXPENSIVE_PREFIXES) else "standard"


class RateLimitMiddleware:
    """Applies the limit before routing, so a throttled request costs no database work."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.settings.rate_limit_enabled:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return
        limiter: RateLimiter | None = getattr(scope["app"].state, "rate_limiter", None)
        if limiter is None:  # limiter is built in the lifespan; nothing to do before it
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        client = scope.get("client")
        identity = identity_of(headers, client[0] if client else "unknown", self.settings)
        bucket = bucket_for(path)
        if bucket == "expensive":
            per_minute, burst = (
                self.settings.rate_limit_expensive_per_minute,
                self.settings.rate_limit_expensive_burst,
            )
        else:
            per_minute, burst = (
                self.settings.rate_limit_per_minute,
                self.settings.rate_limit_burst,
            )
        decision = await limiter.check(identity, bucket, per_minute, burst)
        if decision.allowed:
            await self.app(scope, receive, send)
            return

        RATE_LIMITED.labels(bucket=bucket).inc()
        request_id = str(scope.get("state", {}).get("request_id", "unknown"))
        log.warning("rate_limited", bucket=bucket, path=path, retry_after=decision.retry_after)
        body = (
            b'{"error":{"code":"rate_limited","message":"too many requests; retry after '
            + str(decision.retry_after).encode()
            + b's","request_id":"'
            + request_id.encode()
            + b'"}}'
        )
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"retry-after", str(decision.retry_after).encode()),
                    (b"x-ratelimit-bucket", bucket.encode()),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
