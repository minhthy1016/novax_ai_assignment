# D-26: Agent orchestration on LangGraph

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- **Implemented as planned.** The VPN flow is "propose → wait for a different person's
  approval → resume → execute exactly once", which maps onto LangGraph's `interrupt()` plus a
  Postgres checkpointer (durable, resumable across restarts).
- Outside the graph, in plain tested code: authorization, typed tool schemas, the
  pending-action store with action hashes, idempotency, and the hash-chained audit log. The
  graph decides *what to do next*; it never decides *whether it is allowed*.
- Routes: `small_talk` (deterministic, no model call), `knowledge`, `tool`, `refuse`. Before
  routing, "hi" was answered with "I couldn't find this in the approved knowledge" because
  every message went to retrieval. Small talk still cannot state anything about company
  knowledge - its reply is a fixed capability sentence.
- The router's output is **validated before it can act**: unknown tool names, malformed JSON
  or prose all fall back to the knowledge path, which cannot act.
- A conversation is one durable thread, so a paused approval survives a restart and resumes
  with the approver's decision - that is how the requester's conversation gains the final
  answer.
- **Cost:** one extra dependency and one extra model call per turn (visible in `llm_usage`).
  A hand-written state machine would have avoided both.
