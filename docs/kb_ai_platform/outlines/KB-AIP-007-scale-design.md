```yaml
document_id: KB-AIP-007
title: Scale Design on AWS
department: ai_platform
classification: internal
status: outline
derived_from:
  - docs/decisions/D-60-scale-proposal-aws.md
  - evaluation/capacity.py
```

# Scale Design on AWS

**Scope:** how the same architecture grows to thousands of employees, a million documents
and a GPU inference tier. This page covers the design. The sizing figures are generated
by `evaluation/capacity.py` and are not repeated here, so they cannot go stale in two
places.

## 1. What stays the same
- **The same tiers and the same rules:**
  - isolation is enforced in the database;
  - the backend decides what runs;
  - confidential text never leaves the private network.
- **Each tier scales on its own.**

## 2. The AWS layout
- **Edge:** DNS, a web application firewall, a load balancer.
- **API and workers:** containers across three availability zones.
- **Inference:** vLLM on GPU nodes inside the VPC, with continuous batching.
- **Data:**
  - Aurora PostgreSQL with pgvector, with readers for retrieval;
  - S3 for documents, with a key per department;
  - the audit archive under object lock.
- **Queues:** SQS for ingestion, with a dead-letter queue.
- **Hosted models** (managed APIs) are a fallback for public and internal context only.

## 3. A question's path at scale
- The load balancer routes to an API task, which checks the token and loads permissions.
- Retrieval runs on a reader, only in the caller's department partitions, under row-level
  security.
- The answer comes from vLLM in the VPC. A hosted model is used only when the context
  allows egress and the local model is failing.
- Citations are checked; usage and audit are written; the audit chain is archived.

## 4. The one change the scale forces
- **The vector store is partitioned by department.** A single index for the whole corpus
  would not stay in memory; one per department does, and it matches how isolation already
  works.

## 5. The brief's concerns
- **API scaling and backpressure:**
  - a stateless API; per-caller token buckets;
  - queue-based ingestion;
  - load shedding before the inference queue overflows.
- **Inference:** GPUs scale on queue length with a floor of two nodes; per-model admission
  control; fallback rules unchanged.
- **Embeddings and indexing:**
  - a one-off GPU batch for the initial load, then incremental re-indexing by content hash;
  - atomic version swaps per department partition.
- **Caching:** embedding, retrieval and answer caches whose key always includes the
  caller's access scope and the corpus version. A key without the scope would be a
  cross-department leak.
- **Isolation from ingestion to audit:** the department comes from the uploader's
  permission; row-level security at every read; per-department encryption keys;
  confidential context stays in the VPC.
- **Availability and cost:**
  - multi-zone deployment;
  - the index can be rebuilt from S3, and that rebuild time is part of the recovery
    budget;
  - traces and metrics per request;
  - usage per model call feeds cost reports per department.

## 6. What is not yet known
- **The design is not load-tested.** The measurements that must replace assumptions first
  are named in D-60.

## Excluded from this page
- All sizing numbers (they live in `evaluation/capacity.py` and D-60).
- Measured corpus statistics taken from the sample data.
