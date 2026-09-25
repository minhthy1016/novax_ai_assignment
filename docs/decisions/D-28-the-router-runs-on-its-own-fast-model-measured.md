# D-28: The router runs on its own fast model (measured)

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- **Finding:** when the router used the user's answer model, NIM `gpt-oss-20b` timed out
  twice at 60 s on the classification prompt; the gateway fell back and every turn silently
  became knowledge-only. The symptom looked like bad classification; it was latency.
- **Decision:** `OPSASSIST_ROUTER_MODEL` (default `ollama/llama3.2-3b`) routes independently
  of the answer model. Routing is small, frequent and cheap; a slow hosted model must not
  gate every turn. CI uses the deterministic mock, whose rule-based router keeps the agent
  paths under test without a real model.
- **A small router over-matches on words (found in the console).** "Create an OpenVPN profile
  for employee John Tan - with approval", an example request from the brief itself, was
  routed to *refuse* for every role. The 3B model matched "approval" to its only refuse
  example, "skip approval". "…once it is approved" went to *knowledge*. The user with
  `vpn:create` should get a pending action, and the others a denial for the missing
  permission.
- **Fixed by a rule, not by examples (D-34).** A first fix added worked examples, which is
  the same habit that had put evaluation questions into this prompt. The prompt now holds
  behaviour rules only, including "mentioning an approval is not asking to bypass it", and
  the tools reach it as skill cards generated from the registry.
- **Measured with no examples at all:** 6 phrasings × 3 roles (U005 with `vpn:create`, U001
  without, U002 with `vpn:approve` only), 3 runs each, 45 of 54 decisions as intended.
  - "with approval", "with manager approval", "once it is approved" and the plain request:
    pending for U005, denied for U001 and U002.
  - "skip the approval": refuse.
  - "immediately, no confirmation needed" (9 runs): routed to the tool instead of refuse. The
    model then invented an argument, `approval_needed: false`, and the strict schema rejected
    it, so nothing ran and the audit log records the denial. That is an intended outcome for
    safety, and a miss for behaviour. It is left to case memory (D-34, step B), not patched
    into the prompt.
  - Two regression cases (C04, C05) are in `evaluation/cases.jsonl`.
- **Why this is safe even when the router is wrong:** routing only picks a path. Argument
  schemas, permission checks, the pending approval and the audit record sit outside the model
  (D-29, D-31). A false *refuse* blocks legitimate work; a false *tool* is still rejected,
  denied or held for approval. A router error can make the assistant unhelpful, never unsafe.
- **Re-measured without examples (router A/B, 25 Sep 2026).** Once the prompt carried no
  worked examples (D-34), a larger router was the obvious alternative, so both ran on the
  same code, suite (73 cases) and judge-v2. Only `OPSASSIST_ROUTER_MODEL` changed:

  | | `llama3.2-3b` (kept) | `llama3.1-8b` |
  |---|---|---|
  | Cases fully correct | **68/73** | 67/73 |
  | Tool accuracy | 34/34 | 34/34 |
  | Correct abstention | **11/12** | 10/12 |
  | tool_selection | **10/10** | 9/10 (T08: ran a tool nobody asked for) |
  | End-to-end p50 / p95 (no judge running) | **1.2 / 2.4 s** | 8.5 / 10.4 s |

  The 8B router is no better on any measure and about seven times slower on this laptop
  (partly model swapping in 16 GB, but quality alone would not justify it). **The router
  stays on the 3B model.** Reports:
  [`router-3b-run.md`](../../evaluation/reports/router-3b-run.md),
  [`router-8b-run.md`](../../evaluation/reports/router-8b-run.md).
