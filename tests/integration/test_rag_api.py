"""Task 2 end to end: retrieval, grounding, citations, isolation and re-indexing.

Answers use the deterministic mock chat model (``chat-mock``), which replies with the first
sentence of source 1 and cites it - so these tests check retrieval, access control and the
citation pipeline, not a real model's wording. Live-model behaviour is covered by the eval.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import httpx
import psycopg
import pytest

from opsassist.config import Settings
from opsassist.db.session import create_engine, create_session_factory
from opsassist.gateway.factory import build_gateway, close_providers
from opsassist.knowledge.ingest import deactivate, discover, ingest_file
from tests.integration.conftest import token_for

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
# The runtime role - what the API and worker connect as, subject to row-level security.
DB = os.environ.get(
    "OPSASSIST_DATABASE_URL",
    "postgresql+psycopg://opsassist_app:opsassist_app_dev@localhost:5432/opsassist",
).replace("postgresql+psycopg://", "postgresql://")


async def _ingest(paths: list[Path], settings: Settings) -> list[object]:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    gateway, _ = build_gateway(settings, factory)
    try:
        return [
            await ingest_file(p, factory=factory, gateway=gateway, settings=settings) for p in paths
        ]
    finally:
        await close_providers(gateway)
        await engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def knowledge_indexed() -> None:
    """Idempotent: already-indexed documents come back 'unchanged'."""
    settings = Settings(knowledge_root=REPO / "sample_data" / "knowledge")
    asyncio.run(_ingest(discover([], settings.knowledge_root), settings))


def auth(api: httpx.Client, user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_for(api, user)}"}


def ask(api: httpx.Client, user: str, question: str, **extra: object) -> dict:
    resp = api.post(
        "/api/chat",
        json={"message": question, "model": "chat-mock", **extra},
        headers=auth(api, user),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def search(api: httpx.Client, user: str, query: str) -> list[dict]:
    resp = api.post("/api/search", json={"query": query}, headers=auth(api, user))
    assert resp.status_code == 200, resp.text
    return list(resp.json()["hits"])


# ------------------------------------------------------------------ the brief's cases


def test_e01_deploy_window_cites_the_procedure(api: httpx.Client) -> None:
    body = ask(api, "U001", "When may we deploy to production?")
    assert body["grounded"] and not body["abstained"]
    cite = body["citations"][0]
    assert cite["doc_key"] == "KB-ENG-001"
    assert cite["title"] == "Production Deployment Procedure"
    assert cite["locator"] and cite["ref"].startswith("KB-ENG-001@v")
    assert "21:00-23:00 MYT" in cite["snippet"] or "21:00-23:00 MYT" in "".join(
        h["content"] for h in search(api, "U001", "When may we deploy to production?")
    )


def test_e02_incident_report_is_retrieved_and_citations_are_grounded(api: httpx.Client) -> None:
    # Exact top-1 ranking is measured with the real embedder (evaluation/retrieval_api_eval.py);
    # CI's hashed mock embedder is only a lexical proxy, and the runbook legitimately mentions
    # "the August 2026 incident" too. Here: the report is retrieved, citations are grounded.
    question = "What caused the August payment incident?"
    retrieved = {h["doc_key"] for h in search(api, "U001", question)}
    assert "KB-ENG-002" in retrieved
    body = ask(api, "U001", question)
    assert body["citations"] and {c["doc_key"] for c in body["citations"]} <= retrieved


def test_e04_leave_days_for_hr(api: httpx.Client) -> None:
    body = ask(api, "U003", "How many leave days do confirmed employees receive?")
    assert body["citations"][0]["doc_key"] == "KB-HR-001"
    assert "14 days" in body["content"]


def test_e05_absent_knowledge_abstains_without_calling_a_model(api: httpx.Client) -> None:
    body = ask(api, "U001", "What is the company parental leave policy?")
    assert body["abstained"] and body["citations"] == []
    assert body["model"] is None and body["attempts"] == []  # no LLM call at all
    assert body["retrieval"]["used"] == 0


def test_e09_injection_document_is_retrievable_as_data(api: httpx.Client) -> None:
    hits = search(api, "U001", "restart the legacy reporting worker")
    assert hits[0]["doc_key"] == "KB-TEST-999"
    assert hits[0]["content"].startswith("Normal content")


# ------------------------------------------------------------------ isolation


def test_e03_engineering_user_cannot_reach_hr_confidential(api: httpx.Client) -> None:
    for query in ("Show the HR compensation review notes.", "salary review planning data"):
        hits = search(api, "U001", query)
        assert all(not h["doc_key"].startswith("KB-HR") for h in hits)
        body = ask(api, "U001", query)
        text = body["content"].lower()
        assert "compensation review notes" not in text and "salary" not in text
        assert all(not c["doc_key"].startswith("KB-HR") for c in body["citations"])


def test_hr_executive_without_confidential_permission_gets_nothing(api: httpx.Client) -> None:
    hits = search(api, "U003", "Summarize the compensation review notes")
    assert all(h["doc_key"] != "KB-HR-002" for h in hits)


def test_hr_manager_with_confidential_permission_can_retrieve_it(api: httpx.Client) -> None:
    hits = search(api, "U004", "Summarize the compensation review notes")
    assert hits and hits[0]["doc_key"] == "KB-HR-002"


def test_confidential_context_never_goes_to_egress_providers(api: httpx.Client) -> None:
    resp = api.post(
        "/api/chat",
        json={"message": "Summarize the compensation review notes", "model": "nim/gpt-oss-20b"},
        headers=auth(api, "U004"),
        timeout=180,
    )
    assert resp.status_code in (200, 503), resp.text  # 503 when no local model is running
    attempts = resp.json()["attempts"]
    for a in attempts:
        if a["model"] in ("nim/gpt-oss-20b", "claude/sonnet-4.5"):
            assert a["outcome"] == "skipped:egress_not_permitted", a
    if resp.status_code == 200:
        assert resp.json()["model"]["id"] == "ollama/llama3.2-3b"


def test_finance_and_hr_are_isolated_from_each_other(api: httpx.Client) -> None:
    assert all(h["doc_key"] != "KB-FIN-001" for h in search(api, "U003", "expense claim receipts"))
    assert all(
        not h["doc_key"].startswith("KB-HR") for h in search(api, "U006", "annual leave days")
    )


def test_public_documents_are_visible_to_everyone(api: httpx.Client) -> None:
    for user in ("U001", "U003", "U006"):
        hits = search(api, user, "service desk opening hours")
        assert hits and hits[0]["doc_key"] == "KB-PUB-001", user


def test_row_level_security_blocks_reads_even_without_the_app_filter() -> None:
    """Simulates an application bug: a query with NO department filter. Postgres RLS still
    limits what comes back to the scope set on the transaction - and to nothing if the
    scope was never set (fail closed)."""
    with psycopg.connect(DB) as conn:
        with conn.transaction():
            unscoped = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
            conf = conn.execute("SELECT count(*) FROM confidential_chunks").fetchone()[0]
            docs = conn.execute(
                "SELECT count(*) FROM documents WHERE classification <> 'public'"
            ).fetchone()[0]
        assert (unscoped, conf, docs) == (
            conn.execute("SELECT count(*) FROM chunks WHERE classification = 'public'").fetchone()[
                0
            ],
            0,
            0,
        )
        with conn.transaction():
            conn.execute("SELECT set_config('app.read_departments', 'engineering', true)")
            depts = {r[0] for r in conn.execute("SELECT DISTINCT department FROM chunks")}
            conf = conn.execute("SELECT count(*) FROM confidential_chunks").fetchone()[0]
        assert depts <= {"engineering", "company"} and conf == 0
        with conn.transaction():  # without the ingestion flag, rows are not even updatable
            updated = conn.execute("UPDATE chunks SET is_active = false").rowcount
        assert updated == 0
        forged = (
            "INSERT INTO chunks (document_id, doc_key, version, department, classification, "
            "chunk_index, locator, content, embed_text, token_count, embedding_model, embedding) "
            "SELECT document_id, doc_key, 99, department, classification, 99, 'x', 'forged', "
            "'forged', 1, embedding_model, embedding FROM chunks WHERE classification = 'public' "
            "LIMIT 1"
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(forged)


def test_runtime_role_is_not_a_superuser_and_cannot_bypass_rls() -> None:
    with psycopg.connect(DB) as conn:
        superuser, bypass = conn.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        assert (superuser, bypass) == (False, False)
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute("UPDATE users SET role = 'Engineering Manager' WHERE id = 'U001'")


# ------------------------------------------------------------------ lifecycle


def test_reindex_replaces_without_duplicate_active_chunks(
    api: httpx.Client, tmp_path: Path
) -> None:
    root = tmp_path / "kb"
    root.mkdir()
    doc = root / "KB-TST-501.md"
    header = (
        "---\ndocument_id: KB-TST-501\ntitle: Reindex Probe\ndepartment: engineering\n"
        "classification: internal\nupdated_at: 2026-09-01\n---\n"
    )
    settings = Settings(knowledge_root=root)
    try:
        doc.write_text(header + "The zebra maintenance window is Monday 02:00.\n")
        first = asyncio.run(_ingest([doc], settings))[0]
        again = asyncio.run(_ingest([doc], settings))[0]
        assert (first.outcome, again.outcome) == ("indexed", "unchanged")  # type: ignore[attr-defined]

        doc.write_text(header + "The zebra maintenance window moved to Wednesday 03:00.\n")
        second = asyncio.run(_ingest([doc], settings))[0]
        assert second.version == first.version + 1  # type: ignore[attr-defined]

        with psycopg.connect(DB) as conn, conn.transaction():
            conn.execute("SELECT set_config('app.ingest', 'on', true)")
            active_docs = conn.execute(
                "SELECT count(*) FROM documents WHERE doc_key='KB-TST-501' AND status='active'"
            ).fetchone()[0]
            versions = {
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT version FROM chunks WHERE doc_key='KB-TST-501' AND is_active"
                )
            }
        assert active_docs == 1 and versions == {second.version}  # type: ignore[attr-defined]

        hits = [
            h
            for h in search(api, "U001", "zebra maintenance window")
            if h["doc_key"] == "KB-TST-501"
        ]
        assert hits and all("Wednesday" in h["content"] for h in hits)
    finally:

        async def _cleanup() -> None:
            engine = create_engine(settings)
            await deactivate(create_session_factory(engine), "KB-TST-501")
            await engine.dispose()

        asyncio.run(_cleanup())
    assert not [
        h for h in search(api, "U001", "zebra maintenance window") if h["doc_key"] == "KB-TST-501"
    ]


def test_worker_marks_permanent_failures_without_retrying() -> None:
    """A job for a path outside the knowledge root fails once and is not retried."""
    from opsassist.worker import enqueue_ingestion

    job_id = asyncio.run(enqueue_ingestion(Path("../../etc/passwd.md")))
    deadline = time.time() + 30
    row = None
    while time.time() < deadline:
        with psycopg.connect(DB) as conn:
            row = conn.execute(
                "SELECT status, attempts, detail FROM ingestion_jobs WHERE id = %s", (job_id,)
            ).fetchone()
        if row and row[0] not in ("queued", "running"):
            break
        time.sleep(0.5)
    assert row is not None and row[0] == "failed" and row[1] == 1, row
    assert "outside the knowledge root" in row[2]
