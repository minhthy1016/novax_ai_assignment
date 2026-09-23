# D-01: pgvector in the primary Postgres instead of a dedicated vector DB

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- **Context:** isolation filters must be enforced in a trusted layer, and document versions,
  ACLs and audit records need to stay consistent with the chunks they describe.
- **Decision:** store embeddings in Postgres with pgvector. Filtering happens in the same SQL
  query as similarity search, and Postgres row-level security adds a second storage-layer guard.
- **Alternatives:** Qdrant / Weaviate (better ANN at very large scale, payload filters) —
  but ACL metadata would then live in two systems with no transaction across them.
- **Consequences:** simple and transactional at this size; the scale proposal (§6) covers when
  and how to move to a sharded vector tier.
