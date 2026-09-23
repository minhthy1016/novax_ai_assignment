"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from redis.asyncio import Redis
from starlette.exceptions import HTTPException as StarletteHTTPException

from opsassist import __version__
from opsassist.agent.checkpointer import psycopg_url
from opsassist.agent.graph import AgentDeps, build_graph
from opsassist.api import actions, audit, chat, documents, health, knowledge, memory, models, ui
from opsassist.api import auth as auth_api
from opsassist.api.common import request_id_of
from opsassist.config import Settings, get_settings
from opsassist.db.session import create_engine, create_session_factory
from opsassist.gateway.factory import build_gateway, close_providers
from opsassist.logging_setup import configure_logging, get_logger
from opsassist.middleware import CorrelationMiddleware
from opsassist.ratelimit import RateLimiter, RateLimitMiddleware


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
        # The agent's durable state (paused approvals) lives in Postgres; the tables are
        # created by the migrate step, which runs as the schema owner.
        stack = AsyncExitStack()
        checkpointer = None
        try:
            checkpointer = await stack.enter_async_context(
                AsyncPostgresSaver.from_conn_string(
                    psycopg_url(settings.database_url.get_secret_value())
                )
            )
        except Exception:
            log.warning("checkpointer_unavailable", note="agent runs without durable threads")
        agent = build_graph(AgentDeps(session_factory, gateway, settings), checkpointer)
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.redis = redis
        app.state.rate_limiter = RateLimiter(redis, settings)
        app.state.gateway = gateway
        app.state.provider_statuses = provider_statuses
        app.state.agent = agent
        log.info("startup", env=settings.env, version=__version__)
        try:
            yield
        finally:
            await stack.aclose()
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
    # Starlette runs middleware in reverse registration order: correlation first, so a
    # throttled request still gets a request ID, a log line and an HTTP metric.
    app.add_middleware(RateLimitMiddleware, settings=settings)
    app.add_middleware(CorrelationMiddleware)
    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(models.router)
    app.include_router(knowledge.router)
    app.include_router(actions.router)
    app.include_router(audit.router)
    app.include_router(memory.router)
    app.include_router(documents.router)
    if settings.env in ("dev", "test"):
        # The dev token issuer and the console that uses it live and die together.
        app.include_router(auth_api.router)
        app.include_router(ui.router)

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

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        """Last resort: an unexpected failure still answers in the standard envelope with a
        request ID, and the detail stays in the logs rather than going to the client."""
        request_id = request_id_of(request)
        log.exception("unhandled_error", request_id=request_id, error_type=type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "the request could not be completed",
                    "request_id": request_id,
                }
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
