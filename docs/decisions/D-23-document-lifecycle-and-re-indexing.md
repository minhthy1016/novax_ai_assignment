# D-23: Document lifecycle and re-indexing

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- A content hash over text + metadata makes ingestion idempotent ("unchanged").
  Reclassifying a document changes the hash and re-indexes it into the right table.
- New versions are swapped in atomically: supersede old → deactivate its chunks → insert new,
  in one transaction under a per-document advisory lock. A partial unique index guarantees
  at most one active version per document. Old versions stay (inactive) so past citations
  still resolve. Deletion marks the document `deleted` and deactivates its chunks.
