# D-60: Scale proposal — 5,000 employees, 1M documents, 100 concurrent requests (AWS)

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

The target the brief sets, on the cloud the team confirmed is in production use (AWS). The
shape of the system does not change: the same vertical slice, scaled by making each tier
independently elastic. What changes is that every number below is derived from a measured
quantity in this repository, not assumed.

## Sizing, from the corpus we actually measured

| Quantity | Basis | At 1M documents |
|---|---|---|
| Children per document | measured on the sample corpus: 46 chunks / ~6 pages | ~45M children |
| Vector size | 768-dim `halfvec` (fp16), 1.5 KB + row overhead | **~90 GB** of vectors, ~140 GB with the HNSW graph |
| Answer tokens | measured: ~500 output, ~1,400 prompt tokens per answer | 2.4 M tokens/hour at peak |
| Peak requests | 5,000 employees, 100 concurrent, ~1.8 s p50 answer | ~55 req/s sustained |

**The vector tier is what forces a decision.** 140 GB of index does not stay resident on a
single writer: HNSW must be in memory to keep recall latency flat, so beyond roughly one
`db.r7g.4xlarge` worth of RAM the options are (a) partition, (b) move the vectors out.

## Decisions

**1. API tier — horizontal, stateless, bounded.** ECS Fargate (or EKS) behind an ALB, 6–10
tasks across three AZs. Every request is already async and holds no session state; the
conversation lives in Postgres and the LangGraph checkpoint in the same database, so any
task can resume any conversation — including a paused approval. Scale on concurrent
requests, not CPU: these tasks are I/O-bound on the model.

**2. Backpressure before the GPU, not at it.** A bounded admission semaphore per model with
a maximum queue wait; over it, `429` with `Retry-After` rather than a queue that grows into
timeouts. The per-model circuit breaker that already exists (D-11) is the same mechanism at
the failure end. Order of shedding when the inference tier saturates: batch/evaluation jobs
first, then non-streaming answers, then streaming ones; tool calls and approvals are never
shed, because they are the operations with side effects.

**3. Inference — self-hosted for confidential, managed for the rest.** vLLM on
`g6.12xlarge` nodes in a private subnet, continuous batching, one model per node group.
Sizing from the measured 500 output tokens per answer: 100 concurrent streams at ~25 tok/s
each is ~2,500 output tok/s, which is 3–4 L4-class GPUs for the answer model plus one node
for the router and embedding models, with N+1 for AZ loss. The routing model stays separate
and small — D-28 was learned the hard way, and at 55 req/s a router that shares the answer
queue turns every turn into a queue wait. Managed APIs (Bedrock, NIM) stay configured as
fallback for public and internal classifications only; confidential traffic never leaves the
VPC, which is exactly the egress rule in D-15 expressed as a network boundary.

**4. Vector tier — Aurora PostgreSQL with pgvector, partitioned by department.** Keep
vectors in the relational database (D-01) as long as isolation matters more than raw scale:
row-level security, permissions and document versions stay in one transaction, and the
cross-department leak stays impossible at the storage layer rather than in a filter someone
can forget. At 1M documents this needs three changes:
- `halfvec(768)` instead of `vector(768)` — half the memory for a measured recall cost we
  verify with `evaluation/retrieval_api_eval.py` before switching;
- **partition the chunk tables by department**, so each HNSW index is built and searched per
  partition and the scope filter becomes partition pruning;
- reader endpoints for retrieval, the writer reserved for ingestion and actions.

If the vector tier outgrows that, the next step is OpenSearch or a dedicated store — and
the cost of that step is explicit: isolation stops being enforced by the database, so it has
to be re-established as index-per-department plus a signed scope, and the audit story gets
weaker. That trade is the reason this is step two, not step one.

**5. Ingestion — throughput, then incremental.** The initial 1M-document index is a batch
job, not a user-facing path: ~45M children through a local embedding model on a GPU node is
a few GPU-days at ~2k chunks/s, run on spot capacity with SQS as the queue and S3 as the
document store. Steady state is incremental: only changed documents are re-embedded
(content hash), and the versioned atomic swap (D-23) means a re-index never leaves duplicate
active chunks. Dramatiq on Redis becomes SQS + a worker service; the retry/DLQ semantics
(D-24) carry over unchanged.

**6. Caching, with the scope in every key.** Three boundaries, in increasing risk order:
embedding cache (content hash → vector, immutable, safe to share); retrieval cache
(`question hash + scope hash + corpus version` → chunk ids); answer cache (same key, plus
model id). **A cache key without the access scope is a cross-department leak**, which is why
the scope hash is part of the key and not an afterthought; the corpus version invalidates
the entry when a document is superseded. ElastiCache for both, TTL minutes, and no caching
at all for confidential classifications.

**7. Isolation end to end, unchanged in shape.** Ingestion writes into the department's
partition; retrieval reads under RLS with the runtime role; citations carry the document
version they were retrieved from; the audit chain stays append-only and is exported to S3
Object Lock for retention. Per-department KMS keys make "which department's data" a
key-management question as well as a row-level one.

**8. Availability, DR and cost.** Target 99.9% for answering and 99.95% for tool execution
(the path with side effects). Aurora multi-AZ with PITR: RPO ≈ 5 minutes, RTO ≈ 30 minutes;
cross-region snapshot copies for the documents and audit log. Degradation ladder, in order:
managed-model fallback → smaller local model → retrieval-only answers with citations and no
generation → read-only mode where tools are refused rather than queued. Cost is controlled
by what the usage table already records per attempt: per-department token budgets, a hard
monthly ceiling that trips the same breaker as an overload, spot capacity for ingestion,
and the observation that the cheapest token is the one prompt caching or the retrieval cache
avoids sending.

## What this proposal deliberately does not claim

None of the numbers above are load-tested: they are derived from single-request measurements
in this repository (46 chunks per document, ~1,400 prompt and ~500 output tokens per answer,
1.8 s p50) multiplied out. The first task at real scale is a load test that replaces the
derivations, in this order: vector recall and latency at 45M rows, GPU tokens/s under
continuous batching, and the admission limit at which p95 stops being flat.
