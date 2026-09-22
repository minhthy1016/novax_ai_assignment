"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from starlette.exceptions import HTTPException as StarletteHTTPException

from opsassist import __version__
from opsassist.api import auth as auth_api
from opsassist.api import chat, health, models
from opsassist.api.common import request_id_of
from opsassist.config import Settings, get_settings
from opsassist.db.session import create_engine, create_session_factory
from opsassist.gateway.factory import build_gateway, close_providers
from opsassist.logging_setup import configure_logging, get_logger
from opsassist.middleware import CorrelationMiddleware


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.service_name)
    log = get_logger("opsassist.app")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        session_factory = create_session_factory(engine)
        redis = Redis.from_url(
            settings.redis_url.get_secret_value(),
            socket_timeout=settings.dependency_check_timeout_s,
            socket_connect_timeout=settings.dependency_check_timeout_s,
        )
        gateway, provider_statuses = build_gateway(settings, session_factory)
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.redis = redis
        app.state.gateway = gateway
        app.state.provider_statuses = provider_statuses
        log.info("startup", env=settings.env, version=__version__)
        try:
            yield
        finally:
            await close_providers(gateway)
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
    app.include_router(chat.router)
    app.include_router(models.router)
    if settings.env in ("dev", "test"):
        app.include_router(auth_api.router)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Report where and why, never the rejected input itself (it may contain secrets).
        details = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "request validation failed",
                    "request_id": request_id_of(request),
                },
                "details": details,
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": {401: "unauthorized", 404: "not_found"}.get(
                        exc.status_code, "http_error"
                    ),
                    "message": str(exc.detail),
                    "request_id": request_id_of(request),
                }
            },
            headers=getattr(exc, "headers", None),
        )

    return app
