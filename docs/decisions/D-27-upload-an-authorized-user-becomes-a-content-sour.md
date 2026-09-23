# D-27: Upload: an authorized user becomes a content source

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- Uploading is a *write* into the knowledge base, gated by `kb:write:<department>` (held by
  the three managers in the sample data, candidate-added).
- **Metadata inside the file is untrusted.** The department comes from the permission; a file
  claiming another department is rejected rather than silently corrected. Classification
  defaults to the strictest the uploader may write; `confidential` needs
  `<department>:confidential`; `public` needs a publish permission nobody holds.
- Size and type limits, sanitized names, no execution, and a **credential scan** that rejects
  key-shaped strings.
- Poisoning by a legitimate owner cannot be prevented, so it is made visible: provenance
  (`uploaded_by`) is stored and audited, versions roll back, documents can be deleted.
  Requiring approval before a document joins the index is the next step if that risk
  outweighs convenience.
- Uploads live on a **shared volume** (object storage in production) because the API writes
  them and the worker reads them - found by an integration test, where the first version
  wrote into the API container and the worker failed with "file not found".
