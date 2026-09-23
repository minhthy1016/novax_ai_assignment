# D-14: Usage accounting per attempt

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- One `llm_usage` row per provider attempt — including failures, skips and cancellations —
  with request ID, user, conversation, latency, TTFT, tokens (flagged when estimated) and
  estimated cost. Retries and fallbacks cost money and latency; hiding them hides the bill.
- Costs use reference prices from the catalog (NIM trial usage is free; prices show what the
  same traffic would cost on a paid endpoint).
- Usage writes are shielded from cancellation and never fail the user's request.
