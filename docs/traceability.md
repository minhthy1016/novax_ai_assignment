# Requirement traceability

Every requirement in the brief, mapped to where it is implemented and the evidence that
proves it. Status: ✅ done and verified ·
                   🟡 partial · 
                   ⬜ not started · 
                   ➖ out of scope (explained).

Updated at the end of each build day. A requirement is only ✅ when a test or a reproducible
command demonstrates it — "the code exists" is not enough.

## Task 1 - AI gateway and provider abstraction
| ID | Requirement | Implementation | Evidence | Status |
|---|---|---|---|---|
| T1.1 | `POST /api/chat` | `api/chat.py::chat` | `test_chat_returns_answer_usage_and_persists_conversation`; live NIM + Ollama calls (see PR description) | ✅ |
| T1.2 | `POST /api/chat/stream` | `api/chat.py::chat_stream` (SSE: meta, model, delta, done, error) | `test_stream_event_sequence_and_persistence`, `test_stream_total_failure_emits_error_event`; live Ollama stream | ✅ |
| T1.3 | `POST /api/embeddings` | `api/models.py::embeddings` | `test_embeddings`; live Ollama (768-d) and NIM (2048-d) | ✅ |
| T1.4 | `GET /api/models` | `api/models.py::list_models` (availability, circuit, egress, prices) | `test_models_lists_catalog_with_availability` | ✅ |
| T1.5 | `GET /api/conversations/{id}` | `api/chat.py::get_conversation` (owner-scoped; others get 404) | `test_conversation_of_another_user_is_not_found` | ✅ |
| T1.6 | Configurable provider/model without changing business logic | `config/models.toml` + `gateway/catalog.py`; 3-model picker + per-conversation switching (D-16) | `tests/unit/test_catalog.py`, `test_switching_models_keeps_the_conversation`, `tests/integration/test_models_via_api.py` (every catalog model → 200 or controlled error) | ✅ |
| T1.7 | Streaming, timeouts, retries with backoff, cancellation | `gateway/gateway.py`, `gateway/resilience.py` | `test_transient_errors_are_retried_with_backoff`, `test_timeout_triggers_fallback`, `test_stream_idle_timeout_is_enforced`, `test_stream_cancellation_records_cancelled_usage`; live disconnect → `partial` + `cancelled` | ✅ |
| T1.8 | Usage tracking, correlation IDs, graceful provider failure | `llm_usage` per attempt; `middleware.py`; error envelope with `request_id` | `test_success_records_usage_and_cost`, `test_total_provider_failure_is_a_controlled_503`; live invalid NIM key → fallback to Ollama | ✅ |
| T1.9 | ≥2 provider adapters (one may be mock) | `providers/openai_compat.py` (NIM), `providers/anthropic_provider.py` (Claude, official SDK), `providers/ollama.py` (native), `providers/mock.py` | `tests/unit/test_adapters.py`, `tests/unit/test_anthropic_adapter.py` | ✅ (Claude verified against mocked HTTP only — no API key) |

## Task 2 - RAG knowledge system
| ID | Requirement | Implementation | Evidence | Status |
|---|---|---|---|---|
| T2.1 | Ingest PDF, Markdown, plain text | `knowledge/parsing.py` (front matter / sidecar metadata, pypdf layout mode) | `test_markdown_pdf_and_text_are_parsed_with_metadata`, `test_documents_without_metadata_are_rejected`; worker indexed all 8 sample docs | ✅ |
| T2.2 | Parse → normalize → chunk → embed → index → retrieve → (rerank) → generate | `knowledge/{parsing,chunking,ingest,retrieval}.py`, `rag.py`, worker | `tests/integration/test_rag_api.py` (E01, E02, E04, E05, E09); live E01–E12 on NIM (PR description) | ✅ |
| T2.3 | Citations with title + stable locator | `rag.finalize` (validated against retrieved sources), `KB-ENG-001@v1#¶1–4` | `test_citations_are_validated_against_retrieved_sources`, `test_full_width_citation_markers_are_recognised`, `test_e01_deploy_window_cites_the_procedure` | ✅ |
| T2.4 | Justify chunk size, overlap, embedding model, metadata schema, top-K, reranking | architecture.md D-20, D-21 (calibration table) | measured similarity probes; eval re-measures in D5 | 🟡 justified + calibrated; ablation in D5 |
| T2.5 | Department + sensitivity filters before the model | `policy/access.py`, SQL filter + Postgres RLS (D-22), separate confidential table, egress routing (D-15), runtime role (D-17) | `test_access_matrix`, `test_e03_…`, `test_hr_executive_without_confidential_permission_gets_nothing`, `test_row_level_security_blocks_reads_even_without_the_app_filter`, `test_runtime_role_is_not_a_superuser_and_cannot_bypass_rls`, `test_confidential_context_never_goes_to_egress_providers` | ✅ |
| T2.6 | Replacement / re-index without duplicate active chunks | versioned atomic swap + partial unique index (D-23) | `test_reindex_replaces_without_duplicate_active_chunks` | ✅ |

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
| T6.1 | Malicious document indexed; instructions not followed | KB-TEST-999 indexed as its own chunk; escaped `<source>` blocks; untrusted-data prompt (D-25) | `test_sources_cannot_break_out_of_their_element`, `test_injection_text_is_its_own_chunk`, `test_e09_…`; live E09 on NIM | 🟡 tools-side defenses in D4, eval in D5 |
| T6.2 | Authentication + RBAC/ABAC | `auth.py`: role-bound JWT (user + role re-checked per request), permissions from DB only | `test_every_api_route_requires_a_token`, `test_token_is_rejected_after_role_change`, `test_token_is_rejected_after_deactivation` | 🟡 authN done; authZ policies day 4 |
| T6.3 | Department isolation + least-privilege tools | document isolation done (D-22, D-17) | see T2.5 | 🟡 tools in D4 |
| T6.4 | Secrets management | `config.py` (`SecretStr`, prod guard) | `tests/unit/test_config.py` | 🟡 |
| T6.5 | Input validation, rate limiting, output controls | request-ID validation; bounded, `extra=forbid` request schemas; 422s never echo input | `test_unsafe_request_id_is_replaced`, `test_validation_errors_do_not_echo_input` | 🟡 rate limiting day 4 |
| T6.6 | Tamper-aware audit + redaction | log redaction processor | `test_redaction_masks_secret_like_keys` | 🟡 |
| T6.7 | Tests: indirect injection, cross-department leakage | | | ⬜ |

## Task 7 - Deployment
| ID | Requirement | Implementation | Evidence | Status |
|---|---|---|---|---|
| T7.1 | Containerized, Docker Compose | `Dockerfile`, `docker-compose.yml` | `make up` → all services healthy | ✅ |
| T7.2 | Health, readiness, dependency checks | `api/health.py` | `test_readiness_reports_each_failed_dependency`, `test_api_is_ready_against_real_dependencies` | ✅ |
| T7.3 | JSON logging with correlation IDs | `logging_setup.py`, `middleware.py` | `test_logs_are_single_line_json` | ✅ |
| T7.4 | Metrics: requests, errors, latency, model calls, tool calls, retrieval timing | `metrics.py`: HTTP, model calls/latency/tokens, retrieval latency wired; tool calls defined | `test_metrics_use_route_templates` | 🟡 tool calls in D4 |
| T7.5 | Background worker, retryable tasks | `worker.py` (Dramatiq, retries, dead-letter queue, `ingestion_jobs`) | `test_worker_marks_permanent_failures_without_retrying`; compose `worker` service indexed all sample docs | ✅ |
| T7.6 | Migrations, seed instructions, safe config defaults | `migrations/`, `seed.py`, `.env.example` | `test_seed_is_idempotent` | ✅ |
| T7.7 | Scale proposal (5k users, 1M docs, 100 concurrent, GPU cluster) | | | ⬜ |

## Critical findings (must all be "No" with evidence)
| Finding | Guard | Evidence | Status |
|---|---|---|---|
| Arbitrary command execution | | | ⬜ |
| Cross-department / confidential leakage | SQL filter + RLS under a non-superuser role; confidential table; egress routing | isolation tests in `test_rag_api.py`; live E03 | 🟡 tools in D4 |
| Secrets exposed to model, logs, repo, client | `SecretStr`; word-based log redaction; keys only via env; provider error text never returned to clients | `test_redaction_masks_secret_like_keys`; live check: invalid key string absent from logs | 🟡 |
| Sensitive action without authz + confirmation | | | ⬜ |
| Fabricated tool success, citations, audit | | | ⬜ |

## Demo items
| Demo | Script | Status |
|---|---|---|
| RAG query with citation | E01 via `/api/chat` (README) | 🟡 scripted in D6 |
| Non-sensitive tool call | | ⬜ |
| Sensitive action flow | | ⬜ |
| Prompt injection | E09 via `/api/chat` | 🟡 tool-call variant in D4 |
| Provider failure | `model: demo-failover` / `mock/down`; live: invalid `NVIDIA_API_KEY` → Ollama | 🟡 scripted in `docs/walkthrough.md` day 6 |
| Isolation (Engineering ↛ HR-confidential) | E03 via `/api/chat` + `/api/search` | 🟡 scripted in D6 |
