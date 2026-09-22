"""Background worker (Dramatiq on Redis) for ingestion and other retryable jobs.

Run: ``dramatiq opsassist.worker --processes 1 --threads 2``

Failure handling:
* **Transient** errors (provider timeouts, database hiccups) are retried with exponential
  backoff, up to ``MAX_RETRIES``.
* **Permanent** errors (unparseable file, missing metadata, policy violation) are not
  retried - retrying cannot fix them - and the job is marked ``failed``.
* After the last retry Dramatiq moves the message to the Redis **dead-letter queue**
  (``dramatiq:default.XQ``) and the job is marked ``dead`` for an operator to inspect.
Every attempt is visible in the ``ingestion_jobs`` table.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware import AsyncIO, CurrentMessage
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsassist.config import get_settings
from opsassist.db.models import IngestionJob
from opsassist.db.session import create_engine, create_session_factory
from opsassist.knowledge.ingest import IngestionPolicyError, ingest_file
from opsassist.knowledge.parsing import ParseError
from opsassist.logging_setup import configure_logging, get_logger

MAX_RETRIES = 3
PERMANENT_ERRORS = (ParseError, IngestionPolicyError, FileNotFoundError)

_settings = get_settings()
configure_logging(_settings.log_level, "opsassist-worker")
log = get_logger("opsassist.worker")

broker = RedisBroker(url=_settings.redis_url.get_secret_value())  # type: ignore[no-untyped-call]
broker.add_middleware(AsyncIO())
broker.add_middleware(CurrentMessage())
dramatiq.set_broker(broker)


class _Resources:
    """Created lazily inside the worker's event loop, once per process."""

    factory: async_sessionmaker[AsyncSession] | None = None
    gateway: Any = None

    @classmethod
    def get(cls) -> tuple[async_sessionmaker[AsyncSession], Any]:
        if cls.factory is None:
            from opsassist.gateway.factory import build_gateway

            cls.factory = create_session_factory(create_engine(_settings))
            cls.gateway, _ = build_gateway(_settings, cls.factory)
        return cls.factory, cls.gateway


def _should_retry(retries: int, exc: BaseException) -> bool:
    return retries < MAX_RETRIES and not isinstance(exc, PERMANENT_ERRORS)


async def _update_job(
    factory: async_sessionmaker[AsyncSession], job_id: str, **values: Any
) -> None:
    async with factory() as session, session.begin():
        await session.execute(
            update(IngestionJob).where(IngestionJob.id == uuid.UUID(job_id)).values(**values)
        )


@dramatiq.actor(
    queue_name="default",
    retry_when=_should_retry,
    min_backoff=1_000,
    max_backoff=30_000,
    time_limit=10 * 60_000,
)
async def ingest_document(job_id: str, path: str) -> None:
    factory, gateway = _Resources.get()
    message = CurrentMessage.get_current_message()
    retries = int(message.options.get("retries", 0)) if message else 0
    await _update_job(factory, job_id, status="running", attempts=retries + 1)
    try:
        result = await ingest_file(
            Path(path),
            factory=factory,
            gateway=gateway,
            settings=_settings,
            request_id=f"job-{job_id[:12]}",
        )
    except Exception as exc:
        permanent = isinstance(exc, PERMANENT_ERRORS)
        exhausted = retries >= MAX_RETRIES
        status = "failed" if permanent else ("dead" if exhausted else "queued")
        await _update_job(
            factory, job_id, status=status, detail=f"{type(exc).__name__}: {exc}"[:500]
        )
        log.warning("ingestion_attempt_failed", job_id=job_id, status=status, retries=retries)
        raise
    await _update_job(
        factory,
        job_id,
        status="succeeded" if result.outcome == "indexed" else "unchanged",
        doc_key=result.doc_key,
        version=result.version,
        detail=f"{result.chunks} chunks",
    )


async def enqueue_ingestion(path: Path) -> str:
    engine = create_engine(_settings)
    try:
        factory = create_session_factory(engine)
        job_id = uuid.uuid4()
        async with factory() as session, session.begin():
            session.add(IngestionJob(id=job_id, source_path=str(path), status="queued"))
    finally:
        await engine.dispose()
    ingest_document.send(str(job_id), str(path))
    return str(job_id)
