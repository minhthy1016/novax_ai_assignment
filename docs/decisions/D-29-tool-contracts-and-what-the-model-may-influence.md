# D-29: Tool contracts and what the model may influence

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- Four tools, each a narrow function over typed fields: `search_internal_docs`,
  `get_server_status`, `create_support_ticket` and the sensitive `create_vpn_profile`.
  **No deploy tool, no generic execute/SQL/URL tool** (E11), so "deploy now and skip
  approval" cannot be honoured by any path.
- The model may propose a tool name and arguments. Everything after that is code: schema
  validation with `extra="forbid"`, a permission check against the database, and an audit
  record for allow *and* deny.
- Names resolve server-side (`employee_name` → employee id), so the model never invents an
  identifier; an ambiguous name returns an error asking for the id.
- `create_support_ticket` is idempotent for 10 minutes on (user, title, severity, details).
- Tool results are rendered from the returned data, never summarized by a model, so the
  assistant cannot describe a success that did not happen.
