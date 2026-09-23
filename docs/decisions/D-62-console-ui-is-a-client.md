# D-62: The console is a client, and it lives only in development

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

**Context.** Everything in this system is reachable with `curl`, but a reviewer trying
department isolation, an approval or a fallback by hand needs three terminals and a token
in a shell variable. A page makes the pipeline observable in a minute.

**Decision.** `web/index.html`, one self-contained static file served at `/ui`.

- **It is a client, exactly like Flowise ([D-06](D-06-flowise-is-a-client-not-the-orchestrator.md)) and like `curl`.** It holds no policy,
  no permission table and no credentials; it renders what the API returns. A test asserts
  the page contains no permission strings, so it cannot start deciding what to show.
- **Dev/test only**, mounted next to the development token issuer it signs in with. Both
  disappear together outside dev, and a test proves it: a console without an issuer is a
  login page for nothing, and an issuer without a console is worse.
- **Nothing is loaded from the internet.** No CDN, no fonts, no framework — a third-party
  script inside a page that holds a bearer token is an unnecessary trust relationship. A
  Content-Security-Policy of `default-src 'none'` plus a test enforce it.
- **It shows the machinery, not just the answer**: retrieval candidates with vector and
  full-text scores side by side, the matched passage next to the section the model receives,
  provider attempts including fallbacks, tokens and cost, the audit chain and its
  verification, and a button that approves an action with a *wrong* hash so the refusal can
  be watched rather than described.

**Consequences.** Production needs its own UI; this one is a development and demonstration
tool. The page must keep pace with the API contracts — it is untyped JavaScript, so a
renamed response field shows as a blank cell, which is why the integration tests, not the
page, are the contract's guardrail.

**Alternatives considered.** Flowise (stays as the optional `ui` profile: good for showing a
chat client against the API, but it cannot show retrieval scores, approvals or the audit
chain); Streamlit or a React app (a build step and a dependency tree for a page that must
not have one); Swagger UI alone (already available at `/docs` in dev, but it shows requests,
not the pipeline).
