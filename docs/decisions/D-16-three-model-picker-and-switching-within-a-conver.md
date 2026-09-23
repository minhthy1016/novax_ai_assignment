# D-16: Three-model picker and switching within a conversation

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- `selectable` in the catalog lists the models end users can choose:
  `nim/gpt-oss-20b`, `claude/sonnet-4.5`, `ollama/llama3.2-3b`. Choosing one puts it first
  and the other two follow as fallbacks, so a choice is honoured when possible and the
  response says when it was not (`fallback_used`, per-attempt outcomes).
- The choice is stored on the conversation. Sending `model` switches it; omitting it keeps
  the current model; `PATCH /api/conversations/{id}` switches without a message. History
  is model-independent, so the new model sees the whole conversation (Task 4 conversation
  memory). Each assistant message records which model produced it.
- Outside dev/test only picker models are accepted; routes and mock/demo models are
  dev/test-only. A stored choice later removed from the catalog falls back to the default
  instead of failing the next message.
