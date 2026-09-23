"""Gold retrieval set through the running API (the real production path: access scope,
row-level security, relevance gate, parent-section de-duplication, top-K).

Usage: python -m evaluation.retrieval_api_eval [--api http://127.0.0.1:8000]
"""

from __future__ import annotations

import argparse
import json

import httpx

from evaluation.chunking_eval import ROOT, _norm
from evaluation.client import post


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    cases = [json.loads(x) for x in (ROOT / "evaluation/retrieval_cases.jsonl").open()]
    tokens: dict[str, str] = {}
    recall = {1: 0.0, 3: 0.0, 4: 0.0}
    mrr, misses = 0.0, []
    with httpx.Client(base_url=args.api, timeout=60) as api:
        for case in cases:
            user = case["user"]
            if user not in tokens:
                tokens[user] = post(api, "/api/auth/dev-token", json={"user_id": user}).json()[
                    "access_token"
                ]
            hits = post(
                api,
                "/api/search",
                json={"query": case["query"], "top_k": 4},
                headers={"Authorization": f"Bearer {tokens[user]}"},
            ).json()["hits"]
            ev = [_norm(e) for e in case["evidence"]]
            ctx = [_norm(h["context"]) for h in hits]
            first = next((r for r, c in enumerate(ctx, 1) if any(e in c for e in ev)), None)
            mrr += 1 / first if first else 0
            for k in recall:
                top = " ".join(ctx[:k])
                recall[k] += sum(e in top for e in ev) / len(ev)
            if first != 1:
                misses.append(f"{case['id']}(rank {first or '-'}, hits {len(hits)})")
    n = len(cases)
    print(" ".join(f"recall@{k}={v / n:.3f}" for k, v in recall.items()), f"mrr={mrr / n:.3f}")
    print("not ranked first:", ", ".join(misses) or "-")


if __name__ == "__main__":
    main()
