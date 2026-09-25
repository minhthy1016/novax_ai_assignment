"""Hybrid retrieval with access control applied before anything reaches the model.

Pipeline (D-21):
1. Embed the query with the index embedding model (``input_type=query``).
2. Two candidate lists per permitted table: vector similarity (HNSW, cosine) and full-text
   (tsvector, OR-of-terms so long questions still match). Exact tokens such as
   ``web-prod-03`` or ``14 August`` are where full-text search beats embeddings.
3. Reciprocal Rank Fusion (k=60) merges the lists without calibrating two score scales.
   Matching happens on small chunks; the model receives each chunk's parent section, and
   sibling chunks of the same section are collapsed, so top-K means K distinct sections.
4. Relevance gate on semantic similarity: a candidate survives if its cosine similarity is
   at least the model's ``min_relevance`` AND within ``retrieval_relative_margin`` of the
   best hit. Full-text matches improve *ranking* (via RRF) but cannot admit a chunk on
   their own: single common words ("incident", "notes") otherwise let unrelated chunks in.
   Exact identifiers (server IDs, ticket numbers) are tool lookups, not retrieval.
   Nothing surviving -> the near misses (within ``gate_review_margin`` below the bar) are
   returned separately for a judge to review; the caller abstains only if it admits none.
5. Top-K (default 4).

Isolation is enforced twice, in the same transaction:
* the SQL itself filters by the caller's AccessScope (application layer), and
* ``set_config('app.read_departments' | 'app.confidential_departments', ..., true)``
  makes Postgres row-level security enforce the same scope (storage layer).
The confidential table is not queried at all unless the scope includes a confidential
department.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsassist.config import Settings
from opsassist.gateway.gateway import CallContext, LLMGateway
from opsassist.metrics import RETRIEVAL_LATENCY
from opsassist.policy.access import AccessScope

RRF_K = 60


def _as_int(value: object) -> int | None:
    return int(str(value)) if value is not None else None


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    chunk_id: int
    table: str
    doc_key: str
    version: int
    title: str
    department: str
    classification: str
    locator: str
    parent_id: tuple[str, int | None]  # (document version, section) - the parent's identity
    content: str  # the matched passage (cited as the snippet)
    context: str  # the section handed to the model
    doc_updated_at: str
    similarity: float | None
    fts_rank: float | None
    score: float

    @property
    def stable_ref(self) -> str:
        return f"{self.doc_key}@v{self.version}#{self.locator}"


@dataclass
class _Candidate:
    table: str
    row: dict[str, object]
    rrf: float = 0.0
    similarity: float | None = None
    fts_rank: float | None = None

    def to_chunk(self) -> RetrievedChunk:
        r = self.row
        return RetrievedChunk(
            chunk_id=int(str(r["id"])),
            table=self.table,
            doc_key=str(r["doc_key"]),
            version=int(str(r["version"])),
            title=str(r["title"]),
            department=str(r["department"]),
            classification=str(r["classification"]),
            locator=str(r["locator"]),
            parent_id=(str(r["document_id"]), _as_int(r["parent_index"])),
            content=str(r["content"]),
            context=str(r["context"]),
            doc_updated_at=str(r["doc_updated_at"]),
            similarity=self.similarity,
            fts_rank=self.fts_rank,
            score=self.rrf,
        )


@dataclass
class RetrievalResult:
    chunks: list[RetrievedChunk]
    candidates: int
    below_threshold: int
    latency_ms: float
    embedding_model: str
    tables: list[str] = field(default_factory=list)
    # Filled only when the gate admitted nothing: sections just under the bar, for review.
    near_misses: list[RetrievedChunk] = field(default_factory=list)
    reviewed: int = 0  # near misses a judge looked at
    admitted_by_review: int = 0  # of those, the ones it judged to answer the question

    @property
    def has_confidential(self) -> bool:
        return any(c.classification == "confidential" for c in self.chunks)

    @property
    def max_classification(self) -> str | None:
        """The most sensitive class in the context; decides whether it may leave the box."""
        rank = {"public": 0, "internal": 1, "confidential": 2}
        classes = [c.classification for c in self.chunks]
        return max(classes, key=lambda c: rank.get(c, 2)) if classes else None


async def apply_scope(session: AsyncSession, scope: AccessScope) -> None:
    """Session settings read by the RLS policies (transaction-local)."""
    await session.execute(
        text(
            "SELECT set_config('app.read_departments', :read, true), "
            "set_config('app.confidential_departments', :conf, true)"
        ),
        {
            "read": ",".join(sorted(scope.internal_departments)),
            "conf": ",".join(sorted(scope.confidential_departments)),
        },
    )


_TABLES = frozenset({"chunks", "confidential_chunks"})


def _candidate_sql(table: str, acl_sql: str) -> tuple[str, str]:
    # Identifiers come only from the fixed set above and ACL clauses from this module;
    # every user-influenced value (query text, vector, departments) is a bound parameter.
    if table not in _TABLES:
        raise ValueError(f"unknown chunk table {table!r}")
    base = f"""
        SELECT c.id, c.doc_key, c.version, d.title, c.department, c.classification,
               c.document_id, c.parent_index, c.locator, c.content,
               coalesce(c.context, c.content) AS context,
               d.doc_updated_at, {{score}} AS score
        FROM {table} c JOIN documents d ON d.id = c.document_id
        WHERE c.is_active AND c.embedding_model = :model AND ({acl_sql}) {{extra}}
        ORDER BY {{order}} LIMIT :limit
    """  # noqa: S608
    vector = base.format(
        score="1 - (c.embedding <=> CAST(:qvec AS vector))",
        extra="",
        order="c.embedding <=> CAST(:qvec AS vector)",
    )
    fts = base.format(
        score="ts_rank_cd(c.fts, q)",
        extra="AND c.fts @@ q",
        order="ts_rank_cd(c.fts, q) DESC",
    ).replace(
        f"FROM {table} c JOIN",
        f"FROM {table} c CROSS JOIN LATERAL (SELECT to_tsquery('english', "
        "replace(plainto_tsquery('english', :query)::text, '&', '|')) AS q) tsq JOIN",
    )
    return vector, fts


async def retrieve(
    query: str,
    scope: AccessScope,
    *,
    factory: async_sessionmaker[AsyncSession],
    gateway: LLMGateway,
    settings: Settings,
    ctx: CallContext,
    top_k: int | None = None,
) -> RetrievalResult:
    started = time.perf_counter()
    model_id = settings.index_embedding_model
    spec = gateway.catalog.model(model_id)
    threshold = spec.min_relevance if spec.min_relevance is not None else 0.0
    embedded = await gateway.embed(model_id, [query], "query", ctx)
    qvec = "[" + ",".join(f"{x:.7f}" for x in embedded.result.vectors[0]) + "]"

    tables: list[tuple[str, str, dict[str, object]]] = [
        (
            "chunks",
            "c.classification = 'public' OR c.department = ANY(:depts)",
            {"depts": sorted(scope.internal_departments)},
        )
    ]
    if scope.confidential_departments:
        tables.append(
            (
                "confidential_chunks",
                "c.department = ANY(:depts)",
                {"depts": sorted(scope.confidential_departments)},
            )
        )

    fused: dict[tuple[str, int], _Candidate] = {}
    async with factory() as session, session.begin():
        await apply_scope(session, scope)
        for table, acl_sql, acl_params in tables:
            vector_sql, fts_sql = _candidate_sql(table, acl_sql)
            params = {
                "model": model_id,
                "qvec": qvec,
                "query": query,
                "limit": settings.retrieval_candidates,
                **acl_params,
            }
            for kind, sql in (("vector", vector_sql), ("fts", fts_sql)):
                rows = (await session.execute(text(sql), params)).mappings().all()
                for rank, row in enumerate(rows, start=1):
                    cand = fused.setdefault((table, int(row["id"])), _Candidate(table, dict(row)))
                    cand.rrf += 1.0 / (RRF_K + rank)
                    if kind == "vector":
                        cand.similarity = float(row["score"])
                    else:
                        cand.fts_rank = float(row["score"])

    best = max((c.similarity for c in fused.values() if c.similarity is not None), default=0.0)
    floor = max(threshold, best - settings.retrieval_relative_margin)
    kept: list[RetrievedChunk] = []
    missed: list[RetrievedChunk] = []
    below = 0
    review_floor = threshold - settings.gate_review_margin
    for cand in fused.values():
        if cand.similarity is None or cand.similarity < floor:
            below += 1
            if settings.gate_review_margin and (cand.similarity or 0.0) >= review_floor:
                missed.append(cand.to_chunk())
            continue
        kept.append(cand.to_chunk())
    kept = _distinct_sections(kept)
    near_misses = [] if kept else _distinct_sections(missed)[: settings.gate_review_max]
    elapsed = time.perf_counter() - started
    RETRIEVAL_LATENCY.observe(elapsed)
    return RetrievalResult(
        chunks=kept[: top_k or settings.retrieval_top_k],
        candidates=len(fused),
        below_threshold=below,
        latency_ms=round(elapsed * 1000, 2),
        embedding_model=model_id,
        tables=[t for t, _, _ in tables],
        near_misses=near_misses,
    )


def _distinct_sections(chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """Best-scoring match per parent section, best first. The key is the parent's identity,
    not its locator (locators are display text)."""
    seen: set[tuple[str, str, int | None, str]] = set()
    distinct: list[RetrievedChunk] = []
    for c in sorted(chunks, key=lambda c: c.score, reverse=True):
        key = (c.table, *c.parent_id, c.locator if c.parent_id[1] is None else "")
        if key not in seen:
            seen.add(key)
            distinct.append(c)
    return distinct
