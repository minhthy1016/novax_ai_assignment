# D-30: Server status: field-level policy

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- `server:read` shows identity, environment, status and last check for **every** server;
  **CPU and memory only to the owning department and IT Operations**, because another team's
  utilisation is their capacity information. Non-owners see an explicit "hidden" marker
  rather than a silent omission.
- U003 (HR, no `server:read`) is denied outright, as E07 expects.
