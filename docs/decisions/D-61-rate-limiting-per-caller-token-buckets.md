# D-61: Rate limiting — per-caller token buckets, two budgets, fail open

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

**Context.** The brief requires rate limiting as part of input validation and output
control. The thing worth protecting is not the web tier — FastAPI will happily serve
thousands of cheap requests — but the inference queue and the token bill behind
`/api/chat`, `/api/search` and `/api/documents`.

**Decision.**

- **Token bucket, not a fixed window.** A fixed window lets a caller spend a full budget in
  the last second of one window and again in the first second of the next, i.e. twice the
  intended burst at the worst possible moment. The bucket refills continuously, so burst is
  bounded by `burst` and the sustained rate by `per_minute`.
- **Atomic in Redis, via one Lua script.** Refill, test and take in a single round trip,
  so two concurrent requests from the same caller cannot both see the last token. The script
  also returns the wait, which becomes `Retry-After` — the client never computes its own
  allowance.
- **Two budgets.** `expensive` (60/min, burst 30) covers routes that cost a model call, an
  embedding or an ingestion job; `standard` (300/min, burst 100) covers everything else.
  One shared budget would be either too tight to browse conversations or too loose to
  protect inference. Health, readiness and metrics are never throttled: a probe that gets a
  429 reads as an outage.
- **Identity is the verified token subject**, resolved with the same `decode_token` the
  routes use, falling back to the client address when there is no valid token. A caller
  cannot mint a fresh budget by editing the header, and an unauthenticated flood is limited
  by address.
- **Before routing.** The check runs as ASGI middleware inside the correlation middleware,
  so a throttled request costs no database work but still carries a request ID, a log line
  and an HTTP metric.
- **Fail open.** If Redis is unreachable the request is allowed, logged and counted
  (`opsassist_rate_limiter_unavailable_total`).

**Why fail open, deliberately.** This limiter protects capacity and cost — it is not an
authorization control. Nothing about *who may read what* passes through it: that is the
access scope, row-level security, the confidential index and the tool permissions, none of
which touch Redis. Failing closed would convert a cache outage into an assistant outage
while buying no isolation. The tests state this in the same words, so the choice cannot
quietly rot into "we forgot to handle Redis errors".

**Consequences.**

- Batch clients must behave like batch clients: the evaluation scripts wait out
  `Retry-After` (`evaluation/client.py`) instead of being exempted, so the limiter is
  exercised by every evaluation run rather than only by its own tests.
- A shared egress IP (VPN, office NAT) puts all unauthenticated callers in one bucket.
  Acceptable here because every `/api` route requires a token; the only unauthenticated
  route is the dev token issuer.
- At scale the same buckets move in front of the API (ALB/WAF) for crude floods, while this
  per-identity limit stays as the one that understands who the caller is — see
  [D-60](D-60-scale-proposal-aws.md).

**Alternatives considered.** `slowapi`/`fastapi-limiter` (a dependency for ~80 lines, and
neither expresses "expensive vs cheap" without the same custom policy code); a per-process
in-memory limiter (wrong the moment there are two API replicas — which is the deployment
target); limiting inside each route handler (the work is done by then, and it would be
forgotten on the next route added).

**Tested by.** `tests/unit/test_ratelimit.py` (burst bound, per-caller isolation, bucket
separation, forged-token identity, fail-open) and
`test_expensive_routes_are_rate_limited_per_caller` in `tests/integration/test_tools_api.py`
(real Redis: 429 with `Retry-After`, another user unaffected, cheap routes and probes
unaffected).
