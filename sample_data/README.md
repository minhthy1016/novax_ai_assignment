# Sample data

Fictional seed data from the assignment brief (sections 6-8). No real people, systems or credentials.

| File | Loaded by | Notes |
|---|---|---|
| `departments.json` | `make seed` | Department slugs are the isolation unit. `company` holds company-wide public documents. |
| `users.json` | `make seed` | U001-U006 verbatim from the brief; department names mapped to slugs (`IT Operations` -> `it_ops`). |
| `servers.json` | `make seed` | Server inventory verbatim; `-` for offline CPU/memory stored as `null`. |
| `knowledge/*.md` | ingestion worker | Documents 1-5 verbatim, metadata as front matter. `KB-TEST-999` is the deliberate prompt-injection document and **must** be indexed. |
| `knowledge/KB-PUB-001.md` | ingestion worker | *Candidate-added.* Public service-desk and ticket-severity guide: exercises company-wide access. |
| `knowledge/KB-IT-001.pdf` (+ `.meta.json`) | ingestion worker | *Candidate-added.* VPN policy as a **PDF** (generated from `knowledge_src/KB-IT-001.txt` by `scripts/make_sample_pdf.py`). |
| `knowledge/KB-FIN-001.txt` (+ `.meta.json`) | ingestion worker | *Candidate-added.* Expense policy as **plain text**: exercises Finance isolation. |

Documents added by the candidate (to exercise public-document access, PDF parsing and
re-indexing) are marked `source: candidate-added` in their front matter.
