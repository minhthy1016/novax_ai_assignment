# Requirement traceability

Every requirement in the brief, mapped to where it is implemented and the evidence that
proves it. Status: ✅ done and verified · 🟡 partial · ⬜ not started · ➖ out of scope (explained).

Updated at the end of each build day. A requirement is only ✅ when a test or a reproducible
command demonstrates it — "the code exists" is not enough.

## Task 1 - AI gateway and provider abstraction
| ID | Requirement | Implementation | Evidence | Status |
|---|---|---|---|---|
| T1.1 | `POST /api/chat` | | | ⬜ |
| T1.2 | `POST /api/chat/stream` | | | ⬜ |
| T1.3 | `POST /api/embeddings` | | | ⬜ |
| T1.4 | `GET /api/models` | | | ⬜ |
| T1.5 | `GET /api/conversations/{id}` | | | ⬜ |
| T1.6 | Configurable provider/model without changing business logic | | | ⬜ |
| T1.7 | Streaming, timeouts, retries with backoff, cancellation | | | ⬜ |
| T1.8 | Usage tracking, correlation IDs, graceful provider failure | `middleware.py` (correlation) | `tests/unit/test_health_api.py::test_request_id_is_echoed_or_minted` | 🟡 |
| T1.9 | ≥2 provider adapters (one may be mock) | | | ⬜ |

## Task 2 - RAG knowledge system
| ID | Requirement | Implementation | Evidence | Status |
|---|---|---|---|---|
| T2.1 | Ingest PDF, Markdown, plain text | | | ⬜ |
| T2.2 | Parse → normalize → chunk → embed → index → retrieve → (rerank) → generate | | | ⬜ |
| T2.3 | Citations with document title + stable locator | | | ⬜ |
| T2.4 | Justify chunk size, overlap, embedding model, metadata schema, top-K, reranking | | | ⬜ |
| T2.5 | Department + sensitivity filters before results reach the model | | | ⬜ |
| T2.6 | Replacement / re-index without duplicate active chunks | | | ⬜ |

## Task 3 - Agent and controlled tools
| ID | Requirement | Implementation | Evidence | Status |
|---|---|---|---|---|
| T3.1 | Orchestration decides when a tool is required | | | ⬜ |
| T3.2 | ≥3 tools incl. one sensitive | | | ⬜ |
| T3.3 | Typed input schemas | | | ⬜ |
| T3.4 | Authorization before execution | | | ⬜ |
| T3.5 | Explicit, resumable confirmation for sensitive actions | | | ⬜ |
| T3.6 | Audit: actor, policy decision, arguments, result, timestamp | | | ⬜ |
| T3.7 | Model never gets broad credentials or shell | | | ⬜ |

## Task 4 - Memory
| ID | Requirement | Implementation | Evidence | Status |
|---|---|---|---|---|
| T4.1 | Conversation memory | | | ⬜ |
| T4.2 | Persistent memory of selected, permitted facts | | | ⬜ |
| T4.3 | Token-budget strategy | | | ⬜ |
| T4.4 | Inspect + delete persistent memory; no secrets stored | | | ⬜ |

## Task 5 - Evaluation
| ID | Requirement | Implementation | Evidence | Status |
|---|---|---|---|---|
| T5.1 | ≥30 cases covering all 7 categories | | | ⬜ |
| T5.2 | Answer correctness | | | ⬜ |
| T5.3 | Retrieval relevance (rank-sensitive) | | | ⬜ |
| T5.4 | Citation correctness | | | ⬜ |
| T5.5 | Hallucination / abstention | | | ⬜ |
| T5.6 | Tool accuracy | | | ⬜ |
| T5.7 | Latency + provider timing | | | ⬜ |
| T5.8 | Tokens + estimated cost | | | ⬜ |

## Task 6 - Security
| ID | Requirement | Implementation | Evidence | Status |
|---|---|---|---|---|
| T6.1 | Malicious document indexed; instructions not followed | `sample_data/knowledge/KB-TEST-999.md` | | 🟡 |
| T6.2 | Authentication + RBAC/ABAC | | | ⬜ |
| T6.3 | Department isolation + least-privilege tools | | | ⬜ |
| T6.4 | Secrets management | `config.py` (`SecretStr`, prod guard) | `tests/unit/test_config.py` | 🟡 |
| T6.5 | Input validation, rate limiting, output controls | request-ID validation | `test_unsafe_request_id_is_replaced` | 🟡 |
| T6.6 | Tamper-aware audit + redaction | log redaction processor | `test_redaction_masks_secret_like_keys` | 🟡 |
| T6.7 | Tests: indirect injection, cross-department leakage | | | ⬜ |

## Task 7 - Deployment
| ID | Requirement | Implementation | Evidence | Status |
|---|---|---|---|---|
| T7.1 | Containerized, Docker Compose | `Dockerfile`, `docker-compose.yml` | `make up` → all services healthy | ✅ |
| T7.2 | Health, readiness, dependency checks | `api/health.py` | `test_readiness_reports_each_failed_dependency`, `test_api_is_ready_against_real_dependencies` | ✅ |
| T7.3 | JSON logging with correlation IDs | `logging_setup.py`, `middleware.py` | `test_logs_are_single_line_json` | ✅ |
| T7.4 | Metrics: requests, errors, latency, model calls, tool calls, retrieval timing | `metrics.py` (HTTP wired; others defined) | `test_metrics_use_route_templates` | 🟡 |
| T7.5 | Background worker, retryable tasks | | | ⬜ |
| T7.6 | Migrations, seed instructions, safe config defaults | `migrations/`, `seed.py`, `.env.example` | `test_seed_is_idempotent` | ✅ |
| T7.7 | Scale proposal (5k users, 1M docs, 100 concurrent, GPU cluster) | | | ⬜ |

## Critical findings (must all be "No" with evidence)
| Finding | Guard | Evidence | Status |
|---|---|---|---|
| Arbitrary command execution | | | ⬜ |
| Cross-department / confidential leakage | | | ⬜ |
| Secrets exposed to model, logs, repo, client | `SecretStr`, log redaction, `.gitignore` | unit tests above | 🟡 |
| Sensitive action without authz + confirmation | | | ⬜ |
| Fabricated tool success, citations, audit | | | ⬜ |

## Demo items
| Demo | Script | Status |
|---|---|---|
| RAG query with citation | | ⬜ |
| Non-sensitive tool call | | ⬜ |
| Sensitive action flow | | ⬜ |
| Prompt injection | | ⬜ |
| Provider failure | | ⬜ |
| Isolation (Engineering ↛ HR-confidential) | | ⬜ |
