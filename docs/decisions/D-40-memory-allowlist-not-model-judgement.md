# D-40: Memory: allowlist, not model judgement

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- **Persistent memory** stores only five preference keys (language, timezone, team,
  response_style, default_server), each short and scanned for credentials. A model cannot
  decide that a salary figure is worth remembering. Everything is listed and deletable
  through `/api/memory`.
- **Conversation memory** is the recent turns within a token budget plus a rolling summary of
  older ones. The summary prompt treats the transcript as data and is best-effort: if the
  model is unavailable the previous summary stands and the conversation still works.
