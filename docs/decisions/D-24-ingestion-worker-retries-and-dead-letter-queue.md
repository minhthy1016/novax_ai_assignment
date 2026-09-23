# D-24: Ingestion worker, retries and dead-letter queue

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- Dramatiq on Redis with the AsyncIO middleware. Transient errors retry with exponential
  backoff (1-30 s, 3 retries); permanent ones (parse error, missing metadata, policy
  violation, path outside the root) fail immediately - retrying cannot fix them. Exhausted
  messages go to Dramatiq's dead-letter queue and the job is marked `dead`. Every job and
  attempt is visible in `ingestion_jobs` (`make jobs`).
