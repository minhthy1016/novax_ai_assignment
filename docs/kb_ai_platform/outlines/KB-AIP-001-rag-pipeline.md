```yaml
document_id: KB-AIP-001
title: RAG Pipeline Design
department: ai_platform
classification: internal
status: outline
derived_from:
  - docs/decisions/D-01-pgvector-in-the-primary-postgres-instead-of-a-de.md
  - docs/decisions/D-12-embeddings-never-fall-back-across-models.md
  - docs/decisions/D-20-ingestion-parsing-metadata-chunking-embeddings.md
  - docs/decisions/D-21-retrieval-hybrid-search-fusion-relevance-gate-to.md
  - docs/decisions/D-23-document-lifecycle-and-re-indexing.md
  - docs/decisions/D-24-ingestion-worker-retries-and-dead-letter-queue.md
  - docs/decisions/D-25-retrieved-content-is-untrusted.md
  - docs/decisions/D-27-upload-an-authorized-user-becomes-a-content-sour.md
  - docs/decisions/D-33-answer-escalation.md
  - src/opsassist/knowledge/retrieval.py
  - src/opsassist/rag.py
```

# RAG Pipeline Design

**Scope:** how a document becomes searchable, and how a question becomes a cited answer.

## 1. Overview
- The path: parse → chunk → embed → index → retrieve in the caller's scope → grounded answer
  → citation checks.
- **Storage:** one Postgres with pgvector, not a separate vector database. Reasons: one
  transaction for the access filter and the search; row-level security applies to vectors
  too; one less system to operate.

## 2. Parsing and metadata
- **Formats:** Markdown, plain text, PDF.
- **Required metadata:** document id, title, department, classification, updated date.
  - It comes from front matter (Markdown) or a sidecar `.meta.json` (PDF, text).
  - A document without it is rejected.
- **Tables** are written one row per block, with each value next to its column label, so a
  chunk boundary cannot separate a value from its label.
- **Headings** are kept as a path, and the path travels with every chunk.

## 3. Chunking: parent-child
- **Children** are ~64 tokens, packed inside one section. They are what gets embedded and
  matched.
- **Parents** are the section, up to 256 tokens, never crossing a heading. The parent is
  what the model reads.
  - In short: retrieve narrowly, reason broadly, cite precisely.
- The heading path is prepended to the embedded text.
- **Parent identity** is (document version, parent index), not display text.
- **No overlap, and why:** overlap exists to keep a fact whole across a chunk boundary.
  Here children never cross a heading and the model always receives the whole parent, so
  overlap would only add duplicate rows.
- Changing the chunker changes the content hash, so documents are re-indexed rather than
  mixing chunkings.

## 4. Embeddings
- `nomic-embed-text` (768 dimensions) runs locally, so confidential text is embedded on
  the box.
- Queries and passages use the model's separate input types.
- **Embeddings never fall back to another model:** vectors from different models live in
  different spaces. An embedding route has exactly one target; failures retry, then fail
  loudly.

## 5. Retrieval
- **Two candidate lists:** vector search (HNSW, cosine) and full-text search (tsvector,
  OR of terms), 20 candidates each. Both lists are limited to the caller's access scope.
- **Fusion:** Reciprocal Rank Fusion (k = 60). There is no score calibration between the
  two lists.
- **Relevance gate:**
  - A candidate must reach the embedding model's `min_relevance` **and** be within a
    relative margin (0.10) of the best hit.
  - Full-text matches improve ranking but cannot admit a chunk on their own.
- **Sibling collapse:** top-K = 4 means four distinct **sections**.
- **Near-miss review:**
  - Applies when the gate admits nothing. Up to three sections scoring within 0.10 below
    the bar go to a judge on the router model.
  - Passages it admits reach the answering model, with a note that they came in on review.
  - Otherwise the assistant gives the fixed abstention.
  - Confidential near misses only go to on-box models.
- **No reranker:** RRF already fuses two signals over small candidate sets. A
  cross-encoder over the top ~50 is part of the scale design.

## 6. Generation and citations
- **Sources are untrusted data:**
  - They go in the user turn, inside escaped `<source>` elements; the system prompt gives
    them no authority.
  - A document cannot close its element or forge a system block.
- **Citation markers are checked:**
  - Each marker must point at a source that was provided; invalid markers are stripped
    and counted.
  - Full-width and prose-style markers are normalised first.
- **Re-pointing:** a citation whose source does not contain the figures its sentence states
  is moved to the retrieved source that does.
- **Abstention:**
  - The fixed sentence, plus what to do next: rephrase, upload the document, or raise a
    knowledge-gap ticket.
  - A refusal in the model's own words is recognised as an abstention.
- **Escalation:**
  - Triggered by an uncited answer, or one saying the sources do not cover part of the
    question.
  - The same prompt is tried once on a larger local model, and the second answer is kept
    only if it is cited.
  - Streaming does not escalate.

## 7. Document lifecycle
- **Idempotent:** a content hash over text and metadata; unchanged documents are skipped.
- **Atomic version swap:**
  - In one transaction under a per-document advisory lock: supersede the old version,
    deactivate its chunks, insert the new ones.
  - A partial unique index allows only one active version.
- **Old versions stay inactive,** so past citations still resolve. Deletion marks a
  document deleted and deactivates its chunks.

## 8. Ingestion worker
- Dramatiq on Redis. A job row per upload (`ingestion_jobs`).
- **Transient errors** retry with exponential backoff (1–30 s, 3 retries).
- **Permanent errors** fail at once: parse error, missing metadata, policy violation, a path
  outside the knowledge root.
- Exhausted jobs go to the dead-letter queue and are marked `dead`.

## 9. Uploads
- Gated by `kb:write:<department>`.
- **The department comes from the permission,** never from the file; a file claiming
  another department is rejected.
- **Classification:** defaults to the strictest the uploader may write; `confidential`
  needs `<department>:confidential`.
- **Checks:** size and type limits, sanitised names, a credential scan.
- **Provenance:** the uploader is stored and audited; versions roll back.

## Excluded from this page
- Recall, MRR, Hit@1 or any other measured figure, and the calibration values behind
  `min_relevance`.
- The chunking comparison results (which strategies were tried stays in D-20).
- Gold-set questions, evidence, and any evaluation case.
