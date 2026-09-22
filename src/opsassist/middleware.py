"""Request correlation, request logging and HTTP metrics.

Implemented as pure ASGI middleware (not ``BaseHTTPMiddleware``) so streaming responses
are not buffered and client disconnects propagate as cancellation.
"""

from __future__ import annotations

import re
import time
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from opsassist.logging_setup import get_logger
from opsassist.metrics import HTTP_REQUEST_LATENCY, HTTP_REQUESTS

REQUEST_ID_HEADER = "x-request-id"
# Accept a caller-supplied ID only if it is short and log-safe; otherwise mint our own.
# This prevents log injection through the header.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

log = get_logger("opsassist.http")


def resolve_request_id(incoming: str | None) -> str:
    if incoming and _SAFE_REQUEST_ID.fullmatch(incoming):
        return incoming
    return uuid.uuid4().hex


def _route_template(scope: Scope) -> str:
    """Use the matched route pattern as the metric label to keep cardinality bounded."""
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


class CorrelationMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        request_id = resolve_request_id(headers.get(REQUEST_ID_HEADER))
        scope.setdefault("state", {})["request_id"] = request_id

        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message.setdefault("headers", [])
                message["headers"].append((REQUEST_ID_HEADER.encode(), request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - started
            route = _route_template(scope)
            method = scope["method"]
            HTTP_REQUESTS.labels(method=method, route=route, status=str(status_code)).inc()
            HTTP_REQUEST_LATENCY.labels(method=method, route=route).observe(elapsed)
            if route not in ("/metrics", "/healthz"):
                log.info(
                    "http_request",
                    method=method,
                    route=route,
                    status=status_code,
                    duration_ms=round(elapsed * 1000, 2),
                )
            structlog.contextvars.unbind_contextvars("request_id")
