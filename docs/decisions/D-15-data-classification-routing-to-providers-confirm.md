# D-15: Data-classification routing to providers (confirmed)

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- Every provider declares `data_egress`. The context's **most sensitive classification**
  decides whether a call may leave the box, compared against
  `OPSASSIST_EGRESS_MAX_CLASSIFICATION` (default `internal`).
- **Confidential context never reaches an external model** - confirmed explicitly for Claude
  and similar services. Hosted providers are skipped and reported as
  `skipped:egress_not_permitted`, so only on-box models (Ollama) see the text; if none is
  available the request fails (503) rather than leaking. Verified live: U004's compensation
  question was answered by Ollama with NIM and Claude visibly skipped.
- `internal` material may go to hosted providers today. Setting the variable to `public`
  keeps internal documents on-box as well - one environment variable, no code change.
**Confirmed with the team lead.**

**Update (28 Sep 2026): the rule holds for the whole conversation.** Egress was first decided
per turn, from that turn's sources. A later turn about a public document could then send an
earlier confidential answer, carried in its history, to a hosted model. Now each
conversation records the most sensitive context its answers have used
(`conversations.context_classification`, migration 0009). The record is written right after
retrieval, before any model sees the context, and each turn decides egress on the higher of
that record and its own sources. The rule is one-way: a conversation that has used
confidential context stays on-box. Test:
`test_confidential_context_keeps_the_whole_conversation_on_the_box`, with a fresh
conversation as the control.
