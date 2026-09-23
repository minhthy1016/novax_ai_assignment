# D-05: Metric labels are bounded

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- Route templates (`/api/conversations/{id}`), never raw paths; no user or document IDs as
  labels. Unbounded label cardinality is a common way to take down a metrics backend.
