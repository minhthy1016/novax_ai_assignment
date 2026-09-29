# AI Platform knowledge base: plan and outlines

**Status: outlines for review. Nothing here is ingested yet.**

A small, curated knowledge base about how OpsAssist is designed, for engineers who build and
run it: Data Engineers and AI Engineers in the `ai_platform` department. They will ask the
assistant about the architecture and the design decisions behind it.

## Why curated pages, not the decision records themselves

A decision record (`docs/decisions/D-*.md`) is **history**: the context, the alternatives,
what was measured and what changed. A knowledge page is **reference**: how the system works
today. Engineers asking "how does retrieval work?" want the second.

Several records also quote evaluation questions, reference answers, sample records and
scores. Indexing them would put evaluation content into the answering context, which is
the retrieval version of the problem D-34 removed from the prompts. Curated pages avoid
that by design, rather than by filtering.

## The pages

| Document | Title | Built from |
|---|---|---|
| [KB-AIP-001](outlines/KB-AIP-001-rag-pipeline.md) | RAG Pipeline Design | D-01, D-12, D-20, D-21, D-23, D-24, D-25, D-27, D-33 |
| [KB-AIP-002](outlines/KB-AIP-002-llm-gateway.md) | LLM Gateway and Providers | D-02, D-10 to D-16, D-28 |
| [KB-AIP-003](outlines/KB-AIP-003-agent-and-tools.md) | Agent, Tools and Approvals | D-26, D-28 to D-31, D-34, D-40, D-63 |
| [KB-AIP-004](outlines/KB-AIP-004-security-boundaries.md) | Security and Data Boundaries | D-08, D-15, D-17, D-22, D-25, D-27, D-32, D-40, D-61 |
| [KB-AIP-005](outlines/KB-AIP-005-evaluation-harness.md) | Evaluation Harness Design | D-34, D-50 (method only) |
| [KB-AIP-006](outlines/KB-AIP-006-operations.md) | Operations and Deployment | D-03 to D-07, D-14, D-24, D-61, D-62 |
| [KB-AIP-007](outlines/KB-AIP-007-scale-design.md) | Scale Design on AWS | D-60 (the design, not the assumed figures) |

## Rules every page follows

**Content:**
- **Design only.** How the system works and why, as it stands today.
- **Never:**
  - scores, measurements or run results;
  - evaluation case IDs, questions or reference answers;
  - sample records (people, servers, tickets);
  - anything classified confidential;
  - credentials, or environment values beyond variable names.
- **Stable facts only.** A parameter appears only if it is a design setting (e.g. the
  top-K, the chunk sizes), not a measured outcome.
- **Each page states its own sources** in `derived_from`, as repository paths.

**Enforced by tests,** not only by authoring discipline (to build with the pages):
- **Overlap with the evaluation sets:** the prompt hygiene check extends to these pages. No
  5-word run shared with any evaluation question, no 4-word run shared with a reference
  fact or retrieval evidence.
- **Forbidden content:** no sample-record names, no case-ID patterns, no score patterns
  (like `N/73`).
- **Who can read them:** no evaluation actor (U001–U006) may hold `docs:ai_platform`, so
  none of the existing suites can retrieve these pages.

## Metadata

| Field | Value | Where it comes from |
|---|---|---|
| `document_id` | `KB-AIP-00N` | fixed per page |
| `title` | page title | fixed per page |
| `department` | `ai_platform` | fixed; the department and `docs:ai_platform` already exist |
| `classification` | `internal` | fixed |
| `updated_at` | date | `git log -1 --format=%cs` of **the page file**, filled in by the export step at ingest time, never edited by hand |
| `derived_from` | list of paths | written by the author |
| `reconciled_at` | commit | the commit of the sources the page was last checked against |

**Drift check (CI):** for each page, if any path in `derived_from` has a commit newer than
`reconciled_at`, the check fails. Someone then updates the page, or confirms nothing
changed and moves `reconciled_at`. This is what keeps a curated copy from silently going
stale.

**Source of truth is git.** The pages live in the repository and go through review. The
AI Engineer's `kb:write:ai_platform` upload remains for ad-hoc notes only.

## Readers (to seed with the pages)

| ID | Role | Department | Permissions |
|---|---|---|---|
| U007 | Data Engineer | ai_platform | `docs:ai_platform`, `ticket:create` |
| U008 | AI Engineer | ai_platform | `docs:ai_platform`, `server:read`, `ticket:create`, `kb:write:ai_platform` |

## Next steps after the outlines are approved

1. Draft each page from its sources. An LLM may draft; a person reviews every page before
   it is committed, because an invented detail would be served as fact, with a citation.
2. Add the export step (`scripts/export_kb_ai_platform.py`, `make ingest-kb-aip`), the drift
   check, and the extended hygiene and isolation tests.
3. Seed U007 and U008.
4. Write a small suite for these readers (answerable, cross-department, unanswerable,
   misleading premise). Its questions are written independently of the page text, and it
   is frozen before it first runs.
5. Run it, and run the existing suites once to confirm nothing moved.
