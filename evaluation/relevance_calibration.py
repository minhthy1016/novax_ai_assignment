"""Calibrate the retrieval relevance gate (``min_relevance``) for the index embedder.

Positives: the 34 gold retrieval cases - the similarity of the best chunk that contains the
case's evidence, within the case user's access scope.
Negatives: questions whose answer is NOT in the user's approved knowledge (absent topics,
and topics that exist only in departments the user cannot read) - the best similarity of
anything the user can read.

A good threshold admits every positive and rejects every negative; the report shows the gap
and picks the midpoint. Usage: python evaluation/relevance_calibration.py
"""

from __future__ import annotations

import argparse
import json

from evaluation.chunking_eval import KNOWLEDGE, ROOT, Embedder, _cos, _norm
from opsassist.auth import Principal
from opsassist.knowledge.chunking import ChunkingConfig, chunk_document
from opsassist.knowledge.parsing import parse_file
from opsassist.policy.access import scope_for
from opsassist.providers.mock import hashed_embedding

NEGATIVES = [
    ("U001", "What is the company parental leave policy?"),
    ("U001", "What's the weather in Paris today?"),
    ("U001", "How much annual leave do employees get?"),  # HR-only knowledge
    ("U001", "Summarize the compensation review notes."),  # HR-confidential
    ("U001", "What is the expense claim deadline?"),  # Finance-only
    ("U003", "When may we deploy to production?"),  # Engineering-only
    ("U003", "Summarize the compensation review notes."),  # needs hr:confidential
    ("U006", "What caused the August payment incident?"),  # Engineering-only
    ("U005", "What is the rollback threshold for the search API?"),  # Engineering-only
    ("U001", "Who won the football match last night?"),
    ("U001", "What is our stock price target for next year?"),
    ("U004", "How do I reset my VPN password?"),  # IT-only topic, not in HR scope
]


class MockEmbedder:
    """The deterministic CI embedder, so its threshold is calibrated the same way."""

    def embed(self, texts: list[str], kind: str) -> list[list[float]]:
        return [hashed_embedding(t, 768) for t in texts]

    def save(self) -> None:
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--embedder", choices=["nomic", "mock"], default="nomic")
    args = parser.parse_args()
    cfg = ChunkingConfig()
    docs = [
        parse_file(p) for p in sorted(KNOWLEDGE.iterdir()) if p.suffix in {".md", ".pdf", ".txt"}
    ]
    metas = {d.meta.document_id: d.meta for d in docs}
    chunks = [(d.meta.document_id, c) for d in docs for c in chunk_document(d, cfg)]
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
    emb = Embedder() if args.embedder == "nomic" else MockEmbedder()
    vecs = emb.embed([c.embed_text for _, c in chunks], "document")

    def allowed(user: str) -> list[int]:
        scope = scope_for(principals[user])
        return [
            i
            for i, (k, _) in enumerate(chunks)
            if scope.can_read(metas[k].department, metas[k].classification)
        ]

    positives = []
    for case in (json.loads(x) for x in (ROOT / "evaluation/retrieval_cases.jsonl").open()):
        qv = emb.embed([case["query"]], "query")[0]
        ev = [_norm(e) for e in case["evidence"]]
        sims = [
            _cos(qv, vecs[i])
            for i in allowed(case["user"])
            if any(e in _norm(chunks[i][1].context) for e in ev)
        ]
        positives.append((case["id"], max(sims)))
    negatives = []
    for user, query in NEGATIVES:
        qv = emb.embed([query], "query")[0]
        negatives.append((f"{user}: {query}", max(_cos(qv, vecs[i]) for i in allowed(user))))
    emb.save()

    lo_pos = min(s for _, s in positives)
    hi_neg = max(s for _, s in negatives)
    print(f"positives: min {lo_pos:.3f}  (lowest: {sorted(positives, key=lambda x: x[1])[:3]})")
    print(f"negatives: max {hi_neg:.3f}  (highest: {sorted(negatives, key=lambda x: -x[1])[:3]})")
    if lo_pos > hi_neg:
        print(
            f"separable: gap {lo_pos - hi_neg:.3f}; midpoint threshold {(lo_pos + hi_neg) / 2:.3f}"
        )
    else:
        fp = sum(s >= lo_pos for _, s in negatives)
        print(f"NOT separable: {fp} negatives score above the weakest positive")


if __name__ == "__main__":
    main()
