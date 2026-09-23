# D-21: Retrieval: hybrid search, fusion, relevance gate, top-K

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- Vector (HNSW, cosine) and full-text (tsvector, OR of terms) candidates, 20 each, fused
  with Reciprocal Rank Fusion (k=60). Full-text matters: on the gold set it lifts Recall@1 of
  the first chunker from 0.824 (vector only) to 0.897.
- Sibling children of one section are collapsed, so **top-K = 4 means four distinct
  sections** (~270 tokens of context in the gold set).
- **Relevance gate = cost/noise filter, not the abstention mechanism.** Calibration
  (`evaluation/relevance_calibration.py`) over the 34 gold questions and 12 questions that
  must be abstained: the weakest real match scores 0.610 but two out-of-scope questions score
  up to 0.658 - **no similarity threshold separates them** (the embedder sees the topic as
  related even when the user may not read the answering document). The gate is therefore set
  just below the weakest real match (`min_relevance = 0.60`, relative margin 0.10) so it never
  drops real evidence; abstention on the harder cases happens at generation (grounded prompt),
  and is measured in the evaluation. Clear misses (parental leave, weather) still abstain
  without any model call. Isolation never depends on this gate.
- **Through the live API** (`evaluation/retrieval_api_eval.py`; RLS, gate, dedupe, top-4):
  recall@1 0.941, recall@3 1.000, MRR 0.961.
- **Answer-level** (`evaluation/answer_eval.py`, local `llama3.2-3b`, 34 answerable + 6
  must-abstain cases): citation accuracy 0.912 (95% CI 0.77-0.97), citation validity 0.912,
  every fact present in 0.882 of answers (mean coverage 0.939), wrongly abstained 0/34,
  abstained correctly 6/6. The three citation misses are answers that stated the right fact
  without a marker - a small-model behaviour. Fact coverage is lexical and under-credits
  paraphrase; an LLM judge per claim is the day-5 extension.
- **Reranking:** no cross-encoder. RRF already fuses two signals and the candidate sets are
  small; recall@3 is 1.000 through the API. At the 1M-document scale a cross-encoder over the
  top ~50 is proposed (§6).
- **Nothing admitted -> fixed abstention without calling any model.**
