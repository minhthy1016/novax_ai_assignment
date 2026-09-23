# D-12: Embeddings never fall back across models

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- Vectors from different models live in different spaces; silently switching would corrupt
  retrieval. Embedding routes must have exactly one target (enforced by the catalog
  validator). Embedding failures retry, then fail loudly.
