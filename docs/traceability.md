# Requirement traceability

Every requirement in the brief, mapped to where it is implemented and the evidence that
proves it. Status: ✅ done and verified ·
                   🟡 partial · 
                   ⬜ not started · 
                   ➖ out of scope (explained).

Updated at the end of each build day. A requirement is only ✅ when a test or a reproducible
command demonstrates it — "the code exists" is not enough.

`D-xx` refers to a decision record in [`decisions/`](decisions/README.md), where the
alternatives and the measurement behind each choice are written down.

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
| T2.4 | Justify chunk size, overlap, embedding model, metadata schema, top-K, reranking | chunking and retrieval design (D-20, D-21) | `evaluation/chunking_eval.py` (8 strategies incl. Docling, report `evaluation/reports/chunking.md`), `evaluation/relevance_calibration.py`, `evaluation/retrieval_api_eval.py` (live API: R@1 0.941, R@3 1.000, MRR 0.961) | ✅ |
| T2.5 | Department + sensitivity filters before the model | `policy/access.py`, SQL filter + Postgres RLS (D-22), separate confidential table, egress routing (D-15), runtime role (D-17) | `test_access_matrix`, `test_e03_…`, `test_hr_executive_without_confidential_permission_gets_nothing`, `test_row_level_security_blocks_reads_even_without_the_app_filter`, `test_runtime_role_is_not_a_superuser_and_cannot_bypass_rls`, `test_confidential_context_never_goes_to_egress_providers` | ✅ |
| T2.6 | Replacement / re-index without duplicate active chunks | versioned atomic swap + partial unique index (D-23) | `test_reindex_replaces_without_duplicate_active_chunks` | ✅ |

## Task 3 - Agent and controlled tools
| ID | Requirement | Implementation | Evidence | Status |
|---|---|---|---|---|
| T3.1 | Orchestration decides when a tool is required | `agent/graph.py` (LangGraph: small_talk / knowledge / tool / refuse), router output validated before it can act | `test_router_output_is_validated_before_it_can_act`, `test_e06_…`, `test_e11_no_deployment_tool_exists`, `test_greeting_costs_no_model_call` | ✅ |
| T3.2 | ≥3 tools incl. one sensitive | `tools/registry.py`: search_internal_docs, get_server_status, create_support_ticket, create_vpn_profile (sensitive) | `test_every_tool_declares_its_permission_and_schema` | ✅ |
| T3.3 | Typed input schemas | Pydantic models with `extra="forbid"`; names resolved server-side | `test_invalid_tool_arguments_are_rejected`, `test_vpn_arguments_need_exactly_one_subject_and_a_bounded_duration` | ✅ |
| T3.4 | Authorization before execution | `tools/executor.py::authorize` against DB permissions; field-level policy for server status (D-30) | `test_authorization_is_checked_against_permissions`, `test_e07_tool_is_denied_without_the_permission`, `test_utilisation_is_only_visible_to_the_owner_and_it_ops` | ✅ |
| T3.5 | Explicit, resumable confirmation for sensitive actions | pending action + action hash; approver must differ and hold `vpn:approve`; LangGraph `interrupt()` resumes the conversation | `test_e08_vpn_needs_a_different_authorized_approver`, `test_requester_cannot_approve_their_own_action`, `test_injection_cannot_skip_the_approval_step` | ✅ |
| T3.6 | Audit: actor, policy decision, arguments, result, timestamp | `policy/audit.py` hash-chained, written in the action's transaction, redacted | `test_audit_records_decisions_and_detects_tampering`, `test_audit_hash_covers_content_and_predecessor` | ✅ |
| T3.7 | Model never gets broad credentials or shell | no deploy/execute/SQL tool; tool descriptions carry no secrets; keys only in env | `test_tool_descriptions_expose_no_internals`, `test_e11_no_deployment_tool_exists` | ✅ |

## Task 4 - Memory
| ID | Requirement | Implementation | Evidence | Status |
|---|---|---|---|---|
| T4.1 | Conversation memory | recent turns within a token budget (`memory.load_conversation_memory`) | `test_switching_models_keeps_the_conversation`, `test_e08_…` (resumed conversation) | ✅ |
| T4.2 | Persistent memory of selected, permitted facts | `memory.py` allowlist of five preference keys, credential scan | `test_only_allowlisted_preferences_are_storable`, `test_memory_is_limited_inspectable_and_deletable` | ✅ |
| T4.3 | Token-budget strategy | window + rolling summary written back to the conversation (`update_summary`) | `test_memory_rejects_credentials_and_oversized_values`; summary path exercised by long conversations | 🟡 window + summary implemented; summary quality measured in D5 |
| T4.4 | Inspect + delete persistent memory; no secrets | `GET/PUT/DELETE /api/memory` | `test_memory_is_limited_inspectable_and_deletable` | ✅ |

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
| T6.1 | Malicious document indexed; instructions not followed | escaped `<source>` blocks, untrusted-data prompt (D-25); tools authorized outside the model, sensitive actions always pending (D-31) | `test_sources_cannot_break_out_of_their_element`, `test_e09_…`, `test_injection_cannot_skip_the_approval_step`; live E09 | ✅ (eval in D5) |
| T6.2 | Authentication + RBAC/ABAC | `auth.py`: role-bound JWT (user + role re-checked per request), permissions from DB only | `test_every_api_route_requires_a_token`, `test_token_is_rejected_after_role_change`, `test_token_is_rejected_after_deactivation` | 🟡 authN done; authZ policies day 4 |
| T6.3 | Department isolation + least-privilege tools | documents (D-22, D-17); each tool needs its own permission, field-level policy for server data, upload confined to the uploader's department (D-27) | see T2.5; `test_e07_…`, `test_upload_is_confined_to_the_uploader_department` | ✅ |
| T6.4 | Secrets management | `config.py` (`SecretStr`, prod guard) | `tests/unit/test_config.py` | 🟡 |
| T6.5 | Input validation, rate limiting, output controls | request-ID validation; bounded, `extra=forbid` request schemas; 422s never echo input; **per-caller token buckets in Redis** with a tighter budget for model-backed routes (D-61) | `test_unsafe_request_id_is_replaced`, `test_validation_errors_do_not_echo_input`, `tests/unit/test_ratelimit.py` (6), `test_expensive_routes_are_rate_limited_per_caller` (real Redis: 429 + `Retry-After`, per-caller isolation, probes unaffected) | ✅ |
| T6.6 | Tamper-aware audit + redaction | hash-chained `audit_log`, append-only for the runtime role, redacted arguments/results (D-32); tracebacks no longer log local variables | `test_audit_records_decisions_and_detects_tampering`, `test_runtime_role_cannot_rewrite_the_audit_log`, `test_audit_redacts_secret_like_values` | ✅ |
| T6.7 | Tests: indirect injection, cross-department leakage | 94 tests tagged `security` (`make test-security`): authz, isolation, RLS, injection, approvals, audit integrity, egress, upload guards | `uv run pytest -m security` | ✅ |

## Task 7 - Deployment
| ID | Requirement | Implementation | Evidence | Status |
|---|---|---|---|---|
| T7.1 | Containerized, Docker Compose | `Dockerfile`, `docker-compose.yml` | `make up` → all services healthy | ✅ |
| T7.2 | Health, readiness, dependency checks | `api/health.py` | `test_readiness_reports_each_failed_dependency`, `test_api_is_ready_against_real_dependencies` | ✅ |
| T7.3 | JSON logging with correlation IDs | `logging_setup.py`, `middleware.py` | `test_logs_are_single_line_json` | ✅ |
| T7.4 | Metrics: requests, errors, latency, model calls, tool calls, retrieval timing | `metrics.py`: HTTP, model calls/latency/tokens, retrieval latency; tool outcomes in `audit_log` and structured logs | `test_metrics_use_route_templates` | 🟡 tool-call counter not yet wired to Prometheus |
| T7.5 | Background worker, retryable tasks | `worker.py` (Dramatiq, retries, dead-letter queue, `ingestion_jobs`) | `test_worker_marks_permanent_failures_without_retrying`; compose `worker` service indexed all sample docs | ✅ |
| T7.6 | Migrations, seed instructions, safe config defaults | `migrations/`, `seed.py`, `.env.example` | `test_seed_is_idempotent` | ✅ |
| T7.7 | Scale proposal (5k users, 1M docs, 100 concurrent, GPU cluster) | [D-60](decisions/D-60-scale-proposal-aws.md): capacity derived from measured chunk/token counts; partitioned pgvector, vLLM sizing, backpressure, cache keys carrying the access scope, DR and cost controls | architecture.md §6 summary table; numbers traceable to `evaluation/` measurements, load test named as the next step | ✅ |

## Critical findings (must all be "No" with evidence)
| Finding | Guard | Evidence | Status |
|---|---|---|---|
| Arbitrary command execution | no deploy/execute/SQL/URL tool; every argument a typed field; uploads confined to the knowledge root | `test_e11_no_deployment_tool_exists`, `test_invalid_tool_arguments_are_rejected`, `test_ingestion_is_confined_to_the_knowledge_root` | ✅ |
| Cross-department / confidential leakage | SQL filter + RLS under a non-superuser role; confidential table; egress routing; tools inherit the same scope; uploads cannot cross departments | isolation tests in `test_rag_api.py` and `test_tools_api.py`; live E03 | ✅ |
| Secrets exposed to model, logs, repo, client | `SecretStr`; word-based log redaction; keys only via env; provider error text never returned to clients | `test_redaction_masks_secret_like_keys`; live check: invalid key string absent from logs | 🟡 |
| Sensitive action without authz + confirmation | pending action + action hash + different approver + single execution (D-31) | `test_e08_…`, `test_requester_cannot_approve_their_own_action`, `test_injection_cannot_skip_the_approval_step` | ✅ |
| Fabricated tool success, citations, audit | tool results rendered from real data, never model-summarized; citations validated against retrieved sources; audit written in the action's transaction | `test_partial_tool_payloads_do_not_break_rendering`, `test_citations_are_validated_against_retrieved_sources`, `test_audit_records_decisions_and_detects_tampering` | ✅ |

## Demo items
| Demo | Script | Status |
|---|---|---|
| RAG query with citation | E01 via `/api/chat` (README) | 🟡 scripted in D6 |
| Non-sensitive tool call | E06 via `/api/chat` (README demo step 2) | ✅ |
| Sensitive action flow | E08 request → approve (README demo step 3) | ✅ |
| Prompt injection | E09 via `/api/chat`, plus the tool-call variant (README demo step 4) | ✅ |
| Provider failure | `model: demo-failover` / `mock/down`; live: invalid `NVIDIA_API_KEY` → Ollama | 🟡 scripted in `docs/walkthrough.md` day 6 |
| Isolation (Engineering ↛ HR-confidential) | E03 via `/api/chat` + `/api/search` | 🟡 scripted in D6 |
