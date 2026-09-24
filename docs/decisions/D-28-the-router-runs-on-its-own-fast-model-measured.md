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
  for employee John Tan - with approval" was routed to *refuse* for every role. The 3B model
  matched "approval" to the only refuse example, "skip approval". "…once it is approved" went
  to *knowledge*. Both were wrong: the user with `vpn:create` should get a pending action,
  and the others should get a denial for lacking the permission. The prompt now says that
  *mentioning* an approval describes the normal flow, and that only skipping, overriding or
  not waiting for one is *refuse*. Each side has an example worded differently from the
  failing requests, so the fix is not a memorised answer.
- **Measured:** 6 phrasings × 3 roles (U005 with `vpn:create`, U001 without, U002 with
  `vpn:approve` only), 3 runs each. All 54 decisions were right after the fix: pending, denied
  and denied for "with approval" / "with manager approval" / "once it is approved"; refuse for
  "no confirmation needed" and "skip the approval". Two regression cases (C04, C05) are in
  `evaluation/cases.jsonl`.
- **Why this is safe even when the router is wrong:** routing only picks a path. Permission
  checks, the pending approval and the audit record sit outside the model (D-29, D-31). A
  false *refuse* blocks legitimate work, and a false *tool* is still denied or held for
  approval. A router error can make the assistant unhelpful, never unsafe.
