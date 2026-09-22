"""Structured JSON logging with request correlation.

Every log line is one JSON object. The correlation ID is bound per request through
``structlog.contextvars`` so any code path (orchestrator, provider, tool, worker) logs it
without passing it around. A redaction processor masks secret-looking keys as a last line
of defence; callers must still avoid logging secrets in the first place.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

_SENSITIVE_KEY_PARTS = ("password", "secret", "token", "api_key", "apikey", "authorization")
REDACTED = "[REDACTED]"


def redact_sensitive(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if any(part in key.lower() for part in _SENSITIVE_KEY_PARTS):
            event_dict[key] = REDACTED
    return event_dict


def configure_logging(level: str = "INFO", service: str = "opsassist-api") -> None:
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_sensitive,
    ]
    structlog.configure(
        processors=[
            *shared,
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(service=service)

    # Route stdlib loggers (uvicorn, sqlalchemy, alembic) through the same JSON renderer.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
            foreign_pre_chain=shared,
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    # Access logs are replaced by our own request log line (with correlation ID).
    logging.getLogger("uvicorn.access").disabled = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
