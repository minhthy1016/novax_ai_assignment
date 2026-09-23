# D-20: Ingestion: parsing, metadata, chunking, embeddings

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- **Formats:** Markdown (front matter), plain text and PDF (`pypdf`, layout mode so paragraph
  gaps survive extraction) with a `.meta.json` sidecar. Metadata is **mandatory and
  validated**: a document without department and classification is rejected, never indexed
  as "no ACL". Ingestion is confined to the knowledge root (no arbitrary file reads).
- **Structure recovery:** the parser tracks the full heading path ("Service playbooks >
  Payment API") and carries it across page breaks. PDFs have no markup, so headings are
  detected heuristically (standalone short line, numbered "2.1 API Tier" or title case).
  Before this, PDF chunks had no section context at all and page 2 lost page 1's heading.
- **PDF tables: every row carries its own labels.** Text extraction flattens a table to
  "Tier 2 - platform 24/7 30 minutes 9,600", column names lines away; the 3B model then read
  a SEV1 value off the row above (eval case L04). The parser reads each text run's x
  position, treats lines of ≥3 runs whose columns repeat on the next line as a table (first
  line = header), and rewrites each row as `Tier 2 - platform (SEV1): Hours = 24/7;
  Acknowledge within = 10 minutes; ...`, one block per row so a chunk boundary cannot split
  a row. This is the useful half of what Docling's table serialisation does, for ~80 lines
  and no model. Limits: ruled tables with born-digital text only - no two-column tables, no
  merged or multi-line cells, no header repeated for a table continued on the next page, and
  nothing for scans (Docling's layout model remains the answer there, §6).
- **Metadata schema:** `documents` = doc_key, version, title, department, classification,
  status (active/superseded/deleted), content hash, embedding model, chunker, source path,
  MIME, document date. Chunks denormalize department + classification (filtered in the same
  index scan), plus locator, heading path, page, matched text, parent context, `is_active`.
- **Chunking - measured, not assumed** (`evaluation/chunking_eval.py`, report in
  `evaluation/reports/chunking.md`): 34 gold questions over 10 documents (two long,
  heading-dependent ones added for this), gold evidence is exact source text independent of
  any chunker, same local embedder and access control for every strategy. Hybrid mode (as
  in production):

  | Strategy | Recall@1 | Recall@3 | MRR | Tokens to model @3 |
  |---|---:|---:|---:|---:|
  | structural-64 (first version) | 0.897 | 0.971 | 0.949 | 152 |
  | one chunk per page / whole doc | 0.853 | 1.000 | 0.926 | **941** |
  | fixed window 128/32 (structure-blind) | 0.853 | 1.000 | 0.917 | 401 |
  | Docling HybridChunker, 256 tok (reference) | 0.912 | 0.971 | 0.949 | 263 |
  | hierarchical-128 (Docling-style, ours) | 0.971 | 1.000 | 0.980 | 246 |
  | **parent-child 64/256 (chosen)** | **0.971** | **1.000** | **0.985** | 268 |

  Page-based chunks are the worst trade-off (lower precision, 6x the tokens). Small
  structural chunks lose context (R@1 0.897). Heading-aware strategies win; parent-child ties
  or leads everywhere at the same cost and **decouples matching granularity (small chunks,
  sharp embeddings) from context granularity (the whole section to the model)**, which matters
  more as documents get longer. Caveat: 34 cases - one case moves a metric by ~0.03; the gap
  to the first version is consistent in both vector-only and hybrid modes, the gap between
  the top strategies is not significant.
- Production code contains only parent-child (`knowledge/chunking.py`); the strategies it was
  measured against live in `evaluation/chunkers.py` and reuse the same building blocks, so
  the comparison differs only in the strategy.
- **Docling** was evaluated as a reference (HybridChunker with a tiktoken tokenizer, run in a
  separate environment), not adopted as a dependency: on these documents it did not beat the
  in-house hierarchical chunker, and it brings PyTorch and layout models into the image. For
  scanned PDFs, tables and complex layouts its layout model is the right tool (§6).
- **Parent-child mechanics:** children are packed to ~64 tokens inside a section; the section
  (≤256 tokens, never crossing a heading) is stored as `context`. The full heading path is
  prepended to what is embedded. Each child records `parent_index`, so
  **(document version, parent_index) is the parent's identity** and retrieval collapses
  siblings by that identifier rather than by the locator string, which is display text.
  Changing the chunker changes the content hash, so documents are re-indexed rather than
  silently mixing chunkings.
- **Child-size ablation (32 / 64 / 96 / 128 tokens, parent fixed at 256):** in hybrid mode all
  four score identically (Recall@1 0.971, 95% CI 0.85-0.99); vector-only favours 32 slightly
  (0.912 vs 0.882) but the intervals overlap almost completely. **The gain came from
  heading-aware parents, not from how small the child is.** 64 is kept: same metrics as 32
  with ~20% fewer rows to embed and store.
- **What the numbers do not support:** with 34 cases a single case is ~0.03, so differences
  below roughly three cases are not distinguishable. The claim this evaluation supports is
  "heading-aware parent-child beats page-based and small structural chunking", not "64 is the
  optimal child size" or "we beat Docling in general" - Docling's layout strengths (tables,
  multi-column, scans) were not represented in the corpus at that point. `KB-ENG-005` (tables,
  two columns) was added for that on day 5; see `evaluation/reports/chunking.md`.
- **Embedding model: `nomic-embed-text` via Ollama (768-d, local)** with its task prefixes
  (`search_document:` / `search_query:`). Documents never leave the machine (required for
  confidential material, D-15), zero marginal cost, 768 dims fit pgvector HNSW. NIM
  `nemotron-3-embed-1b` (2048-d) was rejected for the index: data egress, and >2000 dims
  needs `halfvec`. The embedding model is recorded per chunk; changing it forces a re-index
  (D-12). CI uses a deterministic 768-d hashed mock with stopwords removed.
- **Citations:** the stable reference is `KB-ENG-003@v1#§Service playbooks › Payment API
  ¶13–14` (doc, version, section, paragraphs; PDFs add pages). The citation snippet is the
  *matched* passage, so the exact supporting text is shown even though the locator names the
  whole section.
