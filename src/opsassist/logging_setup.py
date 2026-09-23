"""Structured JSON logging with request correlation.

Every log line is one JSON object. The correlation ID is bound per request through
``structlog.contextvars`` so any code path (orchestrator, provider, tool, worker) logs it
without passing it around. A redaction processor masks secret-looking keys as a last line
of defence; callers must still avoid logging secrets in the first place.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from itertools import pairwise
from typing import Any

import structlog
from structlog.tracebacks import ExceptionDictTransformer

# Matched against whole words of a key (split on "_" / "-"), not substrings: "access_token"
# and "jwt_secret" are redacted, but usage counters like "prompt_tokens" are not.
_SENSITIVE_WORDS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "apikey",
        "authorization",
        "credential",
        "credentials",
    }
)
REDACTED = "[REDACTED]"


def is_sensitive_key(key: str) -> bool:
    words = re.split(r"[_\-]+", key.lower())
    if any(w in _SENSITIVE_WORDS for w in words):
        return True
    return any(a == "api" and b == "key" for a, b in pairwise(words))


def redact_sensitive(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if is_sensitive_key(key):
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
            # Structured tracebacks WITHOUT local variables: locals carry request bodies,
            # settings objects and tool arguments, which must not land in logs.
            structlog.processors.ExceptionRenderer(ExceptionDictTransformer(show_locals=False)),
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
