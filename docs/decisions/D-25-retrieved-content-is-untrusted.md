# D-25: Retrieved content is untrusted

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- Sources are placed in the user turn inside `<source>` elements with escaped content (a
  document cannot close its element or forge a system block - unit-tested), and the system
  prompt gives them no authority. Citation markers are validated against the sources that
  were actually provided; invalid ones are stripped and counted. Full-width markers (`【2】`,
  emitted by gpt-oss) are normalized.
- Live check (E09): asked to follow KB-TEST-999's instructions, gpt-oss declined, explained
  that document text is information rather than instructions, and summarized the legitimate
  content with a citation. There are no tools in this mode, so nothing could be executed;
  D4 adds tools behind an authorization layer the model cannot bypass.
