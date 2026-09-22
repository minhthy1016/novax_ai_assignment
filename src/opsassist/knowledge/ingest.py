"""Ingestion: parse -> chunk -> embed -> versioned, atomic swap into the index.

Re-indexing invariant: a document key has at most one active version, and only that
version's chunks are active. A new version is written and the old one superseded in ONE
transaction (readers see either the old or the new set, never both), serialized per
document by an advisory lock. Unchanged content (same hash of text + metadata) is a
no-op, so re-running ingestion is idempotent. Metadata is part of the hash: reclassifying
a document re-indexes it into the right table.

CLI: ``python -m opsassist.knowledge.ingest [PATH ...] [--inline]``
(default: enqueue to the worker; ``--inline`` runs in-process).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsassist.config import Settings, get_settings
from opsassist.db.models import INDEX_DIMENSIONS, Chunk, ConfidentialChunk, Document
from opsassist.gateway.gateway import CallContext, LLMGateway
from opsassist.knowledge.chunking import ChunkingConfig, chunk_document
from opsassist.knowledge.parsing import SUPPORTED_SUFFIXES, ParsedDocument, parse_file
from opsassist.logging_setup import get_logger

log = get_logger("opsassist.ingest")
EMBED_BATCH = 32


class IngestionPolicyError(ValueError):
    """The document may not be indexed as configured (permanent; never retried)."""


@dataclass(frozen=True)
class IngestResult:
    doc_key: str
    version: int
    outcome: Literal["indexed", "unchanged"]
    chunks: int


def chunking_config(settings: Settings) -> ChunkingConfig:
    return ChunkingConfig(
        target_tokens=settings.chunk_target_tokens,
        max_tokens=settings.chunk_max_tokens,
    )


def content_hash(doc: ParsedDocument, chunker: str = "") -> str:
    """Text + metadata + chunking config: changing any of them re-indexes the document."""
    payload = json.dumps(
        {
            "meta": doc.meta.model_dump(mode="json"),
            "blocks": [[b.text, list(b.headings)] for b in doc.blocks],
            "chunker": chunker,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def resolve_within_root(path: Path, root: Path) -> Path:
    """Only files under the configured knowledge root can be ingested: a queued job must
    not be able to read arbitrary files from the worker's filesystem."""
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise IngestionPolicyError(f"{path} is outside the knowledge root")
    if resolved.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise IngestionPolicyError(f"{path.name}: unsupported file type")
    return resolved


async def enable_ingest(session: AsyncSession) -> None:
    await session.execute(text("SELECT set_config('app.ingest', 'on', true)"))


def check_embedding_model(gateway: LLMGateway, model_id: str) -> None:
    spec = gateway.catalog.model(model_id)
    if spec.kind != "embedding" or spec.dimensions != INDEX_DIMENSIONS:
        raise IngestionPolicyError(
            f"index embedding model {model_id} must be a {INDEX_DIMENSIONS}-d embedding model"
        )


async def ingest_file(
    path: Path,
    *,
    factory: async_sessionmaker[AsyncSession],
    gateway: LLMGateway,
    settings: Settings,
    request_id: str | None = None,
) -> IngestResult:
    model_id = settings.index_embedding_model
    check_embedding_model(gateway, model_id)
    source = resolve_within_root(path, settings.knowledge_root)
    doc = parse_file(source)
    meta = doc.meta
    spec = gateway.catalog.model(model_id)
    if (
        gateway.catalog.providers[spec.provider].data_egress
        and meta.classification == "confidential"
    ):
        raise IngestionPolicyError(
            f"{meta.document_id} is confidential and {model_id} sends data off-box"
        )
    cfg = chunking_config(settings)
    digest = content_hash(doc, cfg.chunker_id)

    async with factory() as session, session.begin():
        await enable_ingest(session)
        active = await session.scalar(
            select(Document).where(
                Document.doc_key == meta.document_id, Document.status == "active"
            )
        )
        if (
            active is not None
            and active.content_sha256 == digest
            and active.embedding_model == model_id
        ):
            return IngestResult(meta.document_id, active.version, "unchanged", active.chunk_count)

    chunks = chunk_document(doc, cfg)
    ctx = CallContext(request_id=request_id or f"ingest-{uuid.uuid4().hex[:12]}")
    vectors: list[list[float]] = []
    for start in range(0, len(chunks), EMBED_BATCH):
        batch = [c.embed_text for c in chunks[start : start + EMBED_BATCH]]
        outcome = await gateway.embed(model_id, batch, "passage", ctx)
        vectors.extend(outcome.result.vectors)

    table = ConfidentialChunk if meta.classification == "confidential" else Chunk
    async with factory() as session, session.begin():
        await enable_ingest(session)
        # Serialize concurrent ingestion of the same document key.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": meta.document_id}
        )
        active = await session.scalar(
            select(Document)
            .where(Document.doc_key == meta.document_id, Document.status == "active")
            .with_for_update()
        )
        if (
            active is not None
            and active.content_sha256 == digest
            and active.embedding_model == model_id
        ):
            return IngestResult(meta.document_id, active.version, "unchanged", active.chunk_count)

        latest = await session.scalar(
            select(func.max(Document.version)).where(Document.doc_key == meta.document_id)
        )
        version = (latest or 0) + 1
        if active is not None:
            active.status = "superseded"
            active.superseded_at = func.now()
            await session.flush()  # free the one-active-version slot before inserting
        for chunk_table in (Chunk, ConfidentialChunk):  # classification may have changed
            await session.execute(
                update(chunk_table)
                .where(chunk_table.doc_key == meta.document_id, chunk_table.is_active)
                .values(is_active=False)
            )
        new_doc = Document(
            id=uuid.uuid4(),
            doc_key=meta.document_id,
            version=version,
            title=meta.title,
            department=meta.department,
            classification=meta.classification,
            mime_type=doc.mime_type,
            source_path=str(source.relative_to(settings.knowledge_root.resolve())),
            content_sha256=digest,
            doc_updated_at=meta.updated_at,
            embedding_model=model_id,
            chunker=cfg.chunker_id,
            chunk_count=len(chunks),
        )
        session.add(new_doc)
        await session.flush()
        session.add_all(
            table(
                document_id=new_doc.id,
                doc_key=meta.document_id,
                version=version,
                department=meta.department,
                classification=meta.classification,
                chunk_index=c.index,
                locator=c.locator,
                section=c.section,
                page=c.page_start,
                content=c.text,
                context=c.context if c.context != c.text else None,
                headings=list(c.headings),
                embed_text=c.embed_text,
                token_count=c.token_count,
                embedding_model=model_id,
                embedding=vector,
            )
            for c, vector in zip(chunks, vectors, strict=True)
        )
    log.info(
        "document_indexed",
        doc_key=meta.document_id,
        version=version,
        chunks=len(chunks),
        classification=meta.classification,
        table=table.__tablename__,
    )
    return IngestResult(meta.document_id, version, "indexed", len(chunks))


async def deactivate(factory: async_sessionmaker[AsyncSession], doc_key: str) -> bool:
    """Remove a document from retrieval (kept for citation history)."""
    async with factory() as session, session.begin():
        await enable_ingest(session)
        active = await session.scalar(
            select(Document).where(Document.doc_key == doc_key, Document.status == "active")
        )
        if active is None:
            return False
        active.status = "deleted"
        active.superseded_at = func.now()
        for chunk_table in (Chunk, ConfidentialChunk):
            await session.execute(
                update(chunk_table)
                .where(chunk_table.doc_key == doc_key, chunk_table.is_active)
                .values(is_active=False)
            )
    return True


def discover(paths: list[Path], root: Path) -> list[Path]:
    targets = paths or [root]
    files: list[Path] = []
    for p in targets:
        if p.is_dir():
            files.extend(sorted(f for f in p.rglob("*") if f.suffix.lower() in SUPPORTED_SUFFIXES))
        else:
            files.append(p)
    return files


async def _run_inline(files: list[Path]) -> int:
    from opsassist.db.session import create_engine, create_session_factory
    from opsassist.gateway.factory import build_gateway, close_providers

    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    gateway, _ = build_gateway(settings, factory)
    failures = 0
    try:
        for f in files:
            try:
                result = await ingest_file(f, factory=factory, gateway=gateway, settings=settings)
                summary = f"{result.doc_key} v{result.version} ({result.chunks} chunks)"
                print(f"{result.outcome:9} {summary}")
            except Exception as exc:  # report every file, fail the run at the end
                failures += 1
                print(f"failed    {f}: {type(exc).__name__}: {exc}")
    finally:
        await close_providers(gateway)
        await engine.dispose()
    return failures


def main() -> None:
    from opsassist.logging_setup import configure_logging

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--inline", action="store_true", help="run in-process, not via the worker")
    args = parser.parse_args()
    settings = get_settings()
    configure_logging(settings.log_level, "opsassist-ingest")
    files = discover(args.paths, settings.knowledge_root)
    if args.inline:
        raise SystemExit(1 if asyncio.run(_run_inline(files)) else 0)
    from opsassist.worker import enqueue_ingestion

    for f in files:
        job_id = asyncio.run(enqueue_ingestion(f))
        print(f"queued    {f} job={job_id}")


if __name__ == "__main__":
    main()
