"""Chunking strategy comparison: evidence recall@k, MRR and context cost.

Every strategy is scored with the same embedder (nomic-embed-text via local Ollama, with the
model's query/document prefixes), the same per-user access control, and the same gold set
(``evaluation/retrieval_cases.jsonl``). Gold evidence is exact source text, independent of
any chunker; a strategy gets credit only when a retrieved chunk *contains* the evidence -
a fact cut in half by a chunk boundary is not found, which is exactly the context loss being
measured.

With 34 cases a single case moves a metric by ~0.03, so every rate is reported with a 95%
Wilson interval and strategies whose intervals overlap are not claimed to differ.

Metrics (k = 1, 3, 5):
* Recall@k  - fraction of a case's evidence spans present in the top-k contexts, averaged.

A fact that lives in a **table cell** has no canonical string form: our extractor renders a
row as ``checkout-api: Funded RPS = 1,200; ...``, Docling as ``checkout-api, Funded RPS =
1,200``. Those cases therefore carry ``evidence_terms`` - a set of terms that must all
appear in the chunk - so the comparison measures retrieval rather than an extractor's
punctuation. The first run of this comparison scored Docling 0/5 on the table cases for
exactly that reason, which was a bug in the gold set, not a finding about Docling.
* Hit@k     - share of cases where at least one evidence span is present.
* MRR       - 1 / rank of the first context containing any evidence span.
* Ctx tok@k - tokens handed to the model for the top-k (distinct contexts), i.e. cost/noise.

Two retrieval modes: vector-only, and hybrid (vector + BM25 fused with RRF, k=60) which
mirrors production.

Usage:
    python -m evaluation.chunking_eval [--docling evaluation/runs/docling_chunks.json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from evaluation import chunkers
from opsassist.auth import Principal
from opsassist.knowledge.chunking import Chunk, ChunkingConfig, chunk_document
from opsassist.knowledge.parsing import DocumentMeta, ParsedDocument, parse_file
from opsassist.policy.access import scope_for
from opsassist.providers.base import estimate_tokens
from opsassist.providers.mock import _STOPWORDS

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = ROOT / "sample_data" / "knowledge"
CACHE = ROOT / "evaluation" / "runs" / "embed_cache.json"
OLLAMA = "http://localhost:11434"
MODEL = "nomic-embed-text"
KS = (1, 3, 5)


@dataclass(frozen=True)
class EvalChunk:
    doc_key: str
    embed_text: str
    context: str  # what the model would receive


# ------------------------------------------------------------------ strategies


def _ours(
    docs: list[ParsedDocument], chunk: Callable[[ParsedDocument], list[Chunk]]
) -> list[EvalChunk]:
    return [EvalChunk(d.meta.document_id, c.embed_text, c.context) for d in docs for c in chunk(d)]


def _fixed_window(docs: list[ParsedDocument], size: int, overlap: int) -> list[EvalChunk]:
    """Structure-blind baseline: sliding window over the document's words."""
    out = []
    for d in docs:
        words = " ".join(b.text for b in d.blocks).split()
        step = max(1, (size - overlap))  # sizes in ~tokens; ~0.75 words per token
        wsize, wstep = int(size * 0.75), int(step * 0.75)
        for start in range(0, max(1, len(words) - int(overlap * 0.75)), wstep):
            text = " ".join(words[start : start + wsize])
            out.append(EvalChunk(d.meta.document_id, f"{d.meta.title}\n{text}", text))
    return out


def _per_page(docs: list[ParsedDocument]) -> list[EvalChunk]:
    """One chunk per PDF page; whole document for Markdown/text."""
    out = []
    for d in docs:
        pages: dict[int | None, list[str]] = {}
        for b in d.blocks:
            pages.setdefault(b.page, []).append(b.text)
        for texts in pages.values():
            text = "\n".join(texts)
            out.append(EvalChunk(d.meta.document_id, f"{d.meta.title}\n{text}", text))
    return out


def _docling(path: Path) -> list[EvalChunk]:
    rows = json.loads(path.read_text())
    return [EvalChunk(r["doc_key"], r["embed_text"], r["text"]) for r in rows]


# ------------------------------------------------------------------ embedding + ranking


class Embedder:
    def __init__(self) -> None:
        self.cache: dict[str, list[float]] = json.loads(CACHE.read_text()) if CACHE.exists() else {}
        self.client = httpx.Client(base_url=OLLAMA, timeout=120)

    def embed(self, texts: list[str], kind: str) -> list[list[float]]:
        prefix = "search_query: " if kind == "query" else "search_document: "
        keys = [hashlib.sha256((prefix + t).encode()).hexdigest() for t in texts]
        missing = [(k, prefix + t) for k, t in zip(keys, texts, strict=True) if k not in self.cache]
        for i in range(0, len(missing), 32):
            batch = missing[i : i + 32]
            resp = self.client.post(
                "/api/embed", json={"model": MODEL, "input": [t for _, t in batch]}
            )
            resp.raise_for_status()
            for (k, _), vec in zip(batch, resp.json()["embeddings"], strict=True):
                self.cache[k] = vec
        return [self.cache[k] for k in keys]

    def save(self) -> None:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(self.cache))


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) or 1.0)


_WORD = re.compile(r"[a-z0-9%]+")


def _terms(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS]


def _bm25(query: str, docs: list[str], k1: float = 1.2, b: float = 0.75) -> list[float]:
    toks = [_terms(d) for d in docs]
    avg = sum(map(len, toks)) / max(1, len(toks))
    df = Counter(t for ts in toks for t in set(ts))
    n = len(docs)
    scores = []
    for ts in toks:
        tf = Counter(ts)
        s = 0.0
        for q in set(_terms(query)):
            if q not in tf:
                continue
            idf = math.log(1 + (n - df[q] + 0.5) / (df[q] + 0.5))
            s += idf * tf[q] * (k1 + 1) / (tf[q] + k1 * (1 - b + b * len(ts) / avg))
        scores.append(s)
    return scores


def wilson(successes: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson interval for a rate - honest error bars on a small gold set."""
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def _contains(context: str, evidence: list[str], terms: list[list[str]]) -> bool:
    return any(e in context for e in evidence) or any(
        all(t in context for t in group) for group in terms
    )


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("\u2011", "-").replace("\u2013", "-")
    return " ".join(text.lower().split())


# ------------------------------------------------------------------ evaluation


def evaluate(
    name: str,
    chunks: list[EvalChunk],
    cases: list[dict[str, object]],
    metas: dict[str, DocumentMeta],
    principals: dict[str, Principal],
    embedder: Embedder,
    mode: str,
) -> dict[str, object]:
    vectors = embedder.embed([c.embed_text for c in chunks], "document")
    totals = {f"recall@{k}": 0.0 for k in KS} | {f"hit@{k}": 0.0 for k in KS}
    totals |= {"mrr": 0.0} | {f"ctx_tokens@{k}": 0.0 for k in KS}
    misses = []
    for case in cases:
        scope = scope_for(principals[str(case["user"])])
        allowed = [
            i
            for i, c in enumerate(chunks)
            if scope.can_read(metas[c.doc_key].department, metas[c.doc_key].classification)
        ]
        qv = embedder.embed([str(case["query"])], "query")[0]
        vec_rank = sorted(allowed, key=lambda i: -_cos(qv, vectors[i]))
        if mode == "hybrid":
            bm = _bm25(str(case["query"]), [chunks[i].embed_text for i in allowed])
            bm_rank = [
                allowed[j] for j in sorted(range(len(allowed)), key=lambda j: -bm[j]) if bm[j] > 0
            ]
            fused: Counter[int] = Counter()
            for ranking in (vec_rank, bm_rank):
                for r, i in enumerate(ranking, start=1):
                    fused[i] += 1 / (60 + r)
            ranking = [i for i, _ in fused.most_common()]
        else:
            ranking = vec_rank

        # Distinct contexts in rank order (parent_child children share a parent).
        contexts: list[str] = []
        for i in ranking:
            if chunks[i].context not in contexts:
                contexts.append(chunks[i].context)
        evidence = [_norm(e) for e in case["evidence"]]  # type: ignore[union-attr]
        terms = [[_norm(t) for t in group] for group in case.get("evidence_terms", [])]  # type: ignore[union-attr]
        first_hit = next(
            (r for r, ctx in enumerate(contexts, 1) if _contains(_norm(ctx), evidence, terms)),
            None,
        )
        totals["mrr"] += 1 / first_hit if first_hit else 0.0
        for k in KS:
            top = " ".join(_norm(c) for c in contexts[:k])
            found = sum(e in top for e in evidence)
            found += sum(all(t in top for t in group) for group in terms)
            totals[f"recall@{k}"] += found / (len(evidence) + len(terms))
            totals[f"hit@{k}"] += 1.0 if found else 0.0
            totals[f"ctx_tokens@{k}"] += sum(estimate_tokens(c) for c in contexts[:k])
        if not first_hit or first_hit > 3:
            misses.append(f"{case['id']} (first hit: {first_hit or '-'})")
    n = len(cases)
    low, high = wilson(totals["hit@1"], n)
    return {
        "strategy": name,
        "mode": mode,
        "chunks": len(chunks),
        **{k: round(v / n, 3) for k, v in totals.items()},
        "hit@1_ci": f"{low:.2f}-{high:.2f}",
        "cases": n,
        "missed_top3": misses,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--docling", type=Path, default=ROOT / "evaluation/runs/docling_chunks.json"
    )
    parser.add_argument("--report", type=Path, default=ROOT / "evaluation/reports/chunking.md")
    args = parser.parse_args()

    files = sorted(p for p in KNOWLEDGE.iterdir() if p.suffix in {".md", ".pdf", ".txt"})
    docs = [parse_file(p) for p in files]
    metas = {d.meta.document_id: d.meta for d in docs}
    cases = [json.loads(line) for line in (ROOT / "evaluation/retrieval_cases.jsonl").open()]
    users = json.loads((ROOT / "sample_data/users.json").read_text())
    principals = {
        u["id"]: Principal(
            user_id=u["id"],
            name=u["name"],
            department=u["department"],
            role=u["role"],
            permissions=frozenset(u["permissions"]),
        )
        for u in users
    }
    # Gold-set sanity: every evidence span must exist verbatim in its document, and every
    # term of a table fact must be findable too.
    by_key = {d.meta.document_id: _norm(" ".join(b.text for b in d.blocks)) for d in docs}
    for c in cases:
        for e in c["evidence"]:
            assert _norm(e) in by_key[c["expected_doc"]], f"{c['id']}: evidence not in doc: {e}"
        for group in c.get("evidence_terms", []):
            for term in group:
                assert _norm(term) in by_key[c["expected_doc"]], f"{c['id']}: term missing: {term}"

    strategies: dict[str, list[EvalChunk]] = {
        "structural-64 (day-3 baseline)": _ours(docs, lambda d: chunkers.structural(d, 64)),
        "structural-128": _ours(docs, lambda d: chunkers.structural(d, 128)),
        "fixed-window-128/32": _fixed_window(docs, 128, 32),
        "per-page / whole-doc": _per_page(docs),
        "hierarchical-128": _ours(docs, lambda d: chunkers.hierarchical(d, 128)),
        "hierarchical-256": _ours(docs, lambda d: chunkers.hierarchical(d, 256)),
        # Child-size ablation at a fixed 256-token parent: how small should the matched
        # chunk be before precision starts costing recall?
        **{
            f"parent-child-{child}/256{' (production)' if child == 64 else ''}": _ours(
                docs,
                lambda d, c=child: chunk_document(d, ChunkingConfig(c, 256)),  # type: ignore[misc]
            )
            for child in (32, 64, 96, 128)
        },
    }
    if args.docling.exists():
        strategies["docling-hybrid-256"] = _docling(args.docling)

    embedder = Embedder()
    results = []
    try:
        for mode in ("vector", "hybrid"):
            for name, chunks in strategies.items():
                results.append(evaluate(name, chunks, cases, metas, principals, embedder, mode))
    finally:
        embedder.save()

    lines = [
        "# Chunking strategy comparison",
        "",
        f"{len(cases)} gold cases over {len(docs)} documents; embedder `{MODEL}` (local Ollama);",
        "access control applied per case user. Generated by `python -m evaluation.chunking_eval`.",
        "",
        f"**Read the intervals, not the third decimal.** With {len(cases)} cases one case is",
        "~0.03; strategies whose Hit@1 intervals overlap are not distinguishable here.",
        "",
    ]
    for mode in ("vector", "hybrid"):
        lines += [
            f"## Retrieval mode: {mode}",
            "",
            "| Strategy | Chunks | Recall@1 | Hit@1 95% CI | Recall@3 | Recall@5 | MRR "
            "| Ctx tok@3 | Missed in top-3 |",
            "|---|---:|---:|:---:|---:|---:|---:|---:|---|",
        ]
        for r in (x for x in results if x["mode"] == mode):
            lines.append(
                f"| {r['strategy']} | {r['chunks']} | {r['recall@1']:.3f} | {r['hit@1_ci']} | "
                f"{r['recall@3']:.3f} | {r['recall@5']:.3f} | {r['mrr']:.3f} | "
                f"{r['ctx_tokens@3']:.0f} | {', '.join(r['missed_top3']) or '-'} |"  # type: ignore[arg-type]
            )
        lines.append("")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(lines))
    (ROOT / "evaluation/runs/chunking_results.json").write_text(json.dumps(results, indent=1))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
