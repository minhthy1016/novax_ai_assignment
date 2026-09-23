# D-04: Correlation IDs accept caller input only if log-safe

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- A caller-supplied `X-Request-ID` is kept only if it matches `[A-Za-z0-9._-]{8,64}`;
  otherwise a new ID is minted. This keeps cross-service tracing while blocking log injection.
