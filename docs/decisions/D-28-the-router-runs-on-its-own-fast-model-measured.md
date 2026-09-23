# D-28: The router runs on its own fast model (measured)

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- **Finding:** when the router used the user's answer model, NIM `gpt-oss-20b` timed out
  twice at 60 s on the classification prompt; the gateway fell back and every turn silently
  became knowledge-only. The symptom looked like bad classification; it was latency.
- **Decision:** `OPSASSIST_ROUTER_MODEL` (default `ollama/llama3.2-3b`) routes independently
  of the answer model. Routing is small, frequent and cheap; a slow hosted model must not
  gate every turn. CI uses the deterministic mock, whose rule-based router keeps the agent
  paths under test without a real model.
