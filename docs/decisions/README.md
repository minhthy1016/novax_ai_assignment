# Decision records

Why the system is built this way. The system overview lives in
[`../../README.md`](../../README.md); the engineering architecture in
[`../../architecture.md`](../../architecture.md).

| # | Decision | Area |
|---|---|---|
| [D-01](D-01-pgvector-in-the-primary-postgres-instead-of-a-de.md) | pgvector in the primary Postgres instead of a dedicated vector DB | Storage |
| [D-02](D-02-deterministic-mock-provider-as-a-first-class-ada.md) | Deterministic mock provider as a first-class adapter | Providers |
| [D-03](D-03-readiness-vs-liveness.md) | Readiness vs liveness | Operations |
| [D-04](D-04-correlation-ids-accept-caller-input-only-if-log.md) | Correlation IDs accept caller input only if log-safe | Operations |
| [D-05](D-05-metric-labels-are-bounded.md) | Metric labels are bounded | Operations |
| [D-06](D-06-flowise-is-a-client-not-the-orchestrator.md) | Flowise is a client, not the orchestrator | Scope |
| [D-07](D-07-cubejs-not-used.md) | CubeJS not used | Scope |
| [D-08](D-08-role-bound-jwt-local-issuer-as-a-stand-in-for-th.md) | Role-bound JWT; local issuer as a stand-in for the company IdP | Security |
| [D-10](D-10-provider-abstraction-and-routing.md) | Provider abstraction and routing | Providers |
| [D-11](D-11-retry-fallback-and-circuit-breaking-rules.md) | Retry, fallback and circuit-breaking rules | Providers |
| [D-12](D-12-embeddings-never-fall-back-across-models.md) | Embeddings never fall back across models | Providers |
| [D-13](D-13-streaming-fallback-only-before-the-first-token-c.md) | Streaming: fallback only before the first token; cancellation propagates | Providers |
| [D-14](D-14-usage-accounting-per-attempt.md) | Usage accounting per attempt | Providers |
| [D-15](D-15-data-classification-routing-to-providers-confirm.md) | Data-classification routing to providers (confirmed with the team lead) | Security |
| [D-16](D-16-three-model-picker-and-switching-within-a-conver.md) | Three-model picker and switching within a conversation | Providers |
| [D-17](D-17-least-privilege-runtime-database-role-security-f.md) | Least-privilege runtime database role (security finding) | Security |
| [D-20](D-20-ingestion-parsing-metadata-chunking-embeddings.md) | Ingestion: parsing, metadata, chunking, embeddings | Knowledge |
| [D-21](D-21-retrieval-hybrid-search-fusion-relevance-gate-to.md) | Retrieval: hybrid search, fusion, relevance gate, top-K | Knowledge |
| [D-22](D-22-department-isolation-application-filter-row-leve.md) | Department isolation: application filter + row-level security | Security |
| [D-23](D-23-document-lifecycle-and-re-indexing.md) | Document lifecycle and re-indexing | Knowledge |
| [D-24](D-24-ingestion-worker-retries-and-dead-letter-queue.md) | Ingestion worker, retries and dead-letter queue | Knowledge |
| [D-25](D-25-retrieved-content-is-untrusted.md) | Retrieved content is untrusted | Security |
| [D-26](D-26-agent-orchestration-on-langgraph.md) | Agent orchestration on LangGraph | Agent |
| [D-27](D-27-upload-an-authorized-user-becomes-a-content-sour.md) | Upload: an authorized user becomes a content source | Security |
| [D-28](D-28-the-router-runs-on-its-own-fast-model-measured.md) | The router runs on its own fast model (measured) | Agent |
| [D-29](D-29-tool-contracts-and-what-the-model-may-influence.md) | Tool contracts and what the model may influence | Agent |
| [D-30](D-30-server-status-field-level-policy.md) | Server status: field-level policy | Security |
| [D-31](D-31-sensitive-actions-propose-confirm-execute-once.md) | Sensitive actions: propose, confirm, execute once | Security |
| [D-32](D-32-tamper-evident-audit.md) | Tamper-evident audit | Security |
| [D-34](D-34-prompts-hold-rules-cases-never-enter-prompts.md) | Prompts hold rules; skills come from the registry; cases never enter a prompt | Agent / Evaluation |
| [D-40](D-40-memory-allowlist-not-model-judgement.md) | Memory: allowlist, not model judgement | Memory |
| [D-60](D-60-scale-proposal-aws.md) | Scale proposal: 5,000 employees, 1M documents, 100 concurrent (AWS) | Scale |
| [D-61](D-61-rate-limiting-per-caller-token-buckets.md) | Rate limiting: per-caller token buckets, two budgets, fail open | Operations |
| [D-62](D-62-console-ui-is-a-client.md) | The console is a client, and it lives only in development | Scope |
| [D-63](D-63-tickets-are-operational-data.md) | A ticket is operational data: read by a tool, never from the documents | Agent |
| [D-50](D-50-evaluation-design.md) | Evaluation: deterministic for rules, LLM judge for prose | Evaluation |
