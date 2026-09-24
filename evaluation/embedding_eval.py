"""Embedding model comparison: local nomic-embed-text vs hosted NIM nemotron-3-embed-1b.

Same chunker (production parent-child), same gold set (``retrieval_cases.jsonl``), same
access control and the same scoring as ``chunking_eval.py``; only the embedding model
changes. Vector-only and hybrid (vector + BM25, reciprocal rank fusion) are both reported.

**Confidential documents are excluded, and so are the cases that ask about them.** Embedding
with NIM sends document text to NVIDIA's API, and confidential material may never leave the
machine (D-15). The corpus here is fictional, but running the experiment in a way that would
break that rule on real data would make it the wrong experiment. Both models are scored on
the same reduced set so the comparison stays fair.

Needs ``NVIDIA_API_KEY`` in the environment and a running Ollama. Usage:
    python -m evaluation.embedding_eval
"""

from __future__ import annotations

import hashlib
import json
import os
import time

import httpx

from evaluation.chunking_eval import (
    CACHE,
    KNOWLEDGE,
    ROOT,
    Embedder,
    EvalChunk,
    _ours,
    evaluate,
)
from opsassist.auth import Principal
from opsassist.knowledge.chunking import chunk_document
from opsassist.knowledge.parsing import parse_file

NIM_URL = "https://integrate.api.nvidia.com/v1"
NIM_MODEL = "nvidia/nemotron-3-embed-1b"
NIM_CACHE = CACHE.with_name("embedding_cache_nemotron.json")


class NimEmbedder(Embedder):
    """Same interface as the Ollama embedder; NIM takes query/passage as ``input_type``."""

    def __init__(self) -> None:
        key = os.environ.get("NVIDIA_API_KEY")
        if not key:
            raise SystemExit("NVIDIA_API_KEY is not set")
        self.cache = json.loads(NIM_CACHE.read_text()) if NIM_CACHE.exists() else {}
        self.client = httpx.Client(
            base_url=NIM_URL, timeout=120, headers={"Authorization": f"Bearer {key}"}
        )
        self.seconds = 0.0
        self.calls = 0

    def embed(self, texts: list[str], kind: str) -> list[list[float]]:
        input_type = "query" if kind == "query" else "passage"
        keys = [hashlib.sha256(f"{input_type}:{t}".encode()).hexdigest() for t in texts]
        missing = [(k, t) for k, t in zip(keys, texts, strict=True) if k not in self.cache]
        for i in range(0, len(missing), 16):
            batch = missing[i : i + 16]
            started = time.perf_counter()
            resp = self.client.post(
                "/embeddings",
                json={
                    "model": NIM_MODEL,
                    "input": [t for _, t in batch],
                    "input_type": input_type,
                    "truncate": "END",
                },
            )
            self.seconds += time.perf_counter() - started
            self.calls += 1
            resp.raise_for_status()
            rows = sorted(resp.json()["data"], key=lambda r: r["index"])
            for (k, _), row in zip(batch, rows, strict=True):
                self.cache[k] = row["embedding"]
        return [self.cache[k] for k in keys]

    def save(self) -> None:
        NIM_CACHE.parent.mkdir(parents=True, exist_ok=True)
        NIM_CACHE.write_text(json.dumps(self.cache))


def main() -> None:
    files = sorted(p for p in KNOWLEDGE.iterdir() if p.suffix in {".md", ".pdf", ".txt"})
    docs = [d for d in (parse_file(p) for p in files) if d.meta.classification != "confidential"]
    kept = {d.meta.document_id for d in docs}
    metas = {d.meta.document_id: d.meta for d in docs}
    all_cases = [json.loads(line) for line in (ROOT / "evaluation/retrieval_cases.jsonl").open()]
    cases = [c for c in all_cases if c["expected_doc"] in kept]
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
    chunks: list[EvalChunk] = _ours(docs, chunk_document)
    nim = NimEmbedder()
    embedders = {"nomic-embed-text (768-d, local)": Embedder(), f"{NIM_MODEL} (2048-d, NIM)": nim}

    rows = []
    for name, embedder in embedders.items():
        for mode in ("vector", "hybrid"):
            rows.append(evaluate(name, chunks, cases, metas, principals, embedder, mode))
        embedder.save()

    excluded = sorted({c["expected_doc"] for c in all_cases} - kept)
    print(
        f"{len(docs)} documents, {len(chunks)} chunks, {len(cases)}/{len(all_cases)} cases "
        f"(confidential excluded: {', '.join(excluded) or 'none'})"
    )
    print("| Model | Mode | Hit@1 | 95% CI | Recall@3 | Recall@5 | MRR | Missed in top-3 |")
    print("|---|---|---:|:---:|---:|---:|---:|---|")
    for r in rows:
        print(
            f"| {r['strategy']} | {r['mode']} | {r['hit@1']:.3f} | {r['hit@1_ci']} | "
            f"{r['recall@3']:.3f} | {r['recall@5']:.3f} | {r['mrr']:.3f} | "
            f"{', '.join(r['missed_top3']) or '-'} |"  # type: ignore[arg-type]
        )
    if nim.calls:
        per_call = nim.seconds / nim.calls
        print(f"\nNIM: {nim.calls} calls, {nim.seconds:.1f}s, {per_call:.2f}s per call")


if __name__ == "__main__":
    main()
