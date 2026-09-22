"""Liveness, readiness and metrics endpoints.

``/healthz`` answers "is the process alive" and never touches dependencies, so a slow
database cannot get healthy pods killed. ``/readyz`` answers "can this instance serve
traffic" and checks every hard dependency with a bounded timeout, returning 503 with a
per-dependency breakdown when any check fails.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from opsassist.logging_setup import get_logger

router = APIRouter(tags=["health"])
log = get_logger("opsassist.health")


class DependencyStatus(BaseModel):
    ok: bool
    latency_ms: float
    detail: str | None = None


class ReadinessReport(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, DependencyStatus]


async def check_postgres(engine: AsyncEngine) -> str | None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
        has_vector = await conn.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
        )
        if not has_vector:
            return "pgvector extension missing (run migrations)"
        has_schema = await conn.scalar(text("SELECT to_regclass('public.alembic_version')"))
        if has_schema is None:
            return "schema not migrated"
    return None


async def check_redis(redis: Redis) -> str | None:
    await redis.ping()
    return None


async def _timed(
    name: str, probe: Callable[[], Awaitable[str | None]], timeout_s: float
) -> DependencyStatus:
    started = time.perf_counter()
    try:
        problem = await asyncio.wait_for(probe(), timeout=timeout_s)
    except TimeoutError:
        problem = f"timed out after {timeout_s}s"
    except Exception as exc:  # readiness must report, not raise
        # Log the exception type only; connection errors can embed DSNs with credentials.
        log.warning("dependency_check_failed", dependency=name, error_type=type(exc).__name__)
        problem = f"unavailable ({type(exc).__name__})"
    latency = round((time.perf_counter() - started) * 1000, 2)
    return DependencyStatus(ok=problem is None, latency_ms=latency, detail=problem)


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/readyz", response_model=ReadinessReport)
async def readyz(request: Request, response: Response) -> ReadinessReport:
    state = request.app.state
    timeout = state.settings.dependency_check_timeout_s
    postgres, redis = await asyncio.gather(
        _timed("postgres", lambda: check_postgres(state.engine), timeout),
        _timed("redis", lambda: check_redis(state.redis), timeout),
    )
    checks = {"postgres": postgres, "redis": redis}
    ready = all(c.ok for c in checks.values())
    if not ready:
        response.status_code = 503
    return ReadinessReport(status="ready" if ready else "not_ready", checks=checks)


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
