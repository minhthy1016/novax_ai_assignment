"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from opsassist import __version__
from opsassist.api import health
from opsassist.config import Settings, get_settings
from opsassist.db.session import create_engine, create_session_factory
from opsassist.logging_setup import configure_logging, get_logger
from opsassist.middleware import CorrelationMiddleware


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.service_name)
    log = get_logger("opsassist.app")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        redis = Redis.from_url(
            settings.redis_url.get_secret_value(),
            socket_timeout=settings.dependency_check_timeout_s,
            socket_connect_timeout=settings.dependency_check_timeout_s,
        )
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        app.state.redis = redis
        log.info("startup", env=settings.env, version=__version__)
        try:
            yield
        finally:
            await redis.aclose()
            await engine.dispose()
            log.info("shutdown")

    app = FastAPI(
        title="OpsAssist - AI Operations Assistant",
        version=__version__,
        lifespan=lifespan,
        # Interactive docs are a development convenience, not a production surface.
        docs_url="/docs" if settings.env in ("dev", "test") else None,
        redoc_url=None,
    )
    app.add_middleware(CorrelationMiddleware)
    app.include_router(health.router)
    return app
