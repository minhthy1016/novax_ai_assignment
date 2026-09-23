"""Answer-level evaluation through the running API: citations, facts, abstention.

Retrieval metrics say whether the evidence was *found*; these say whether the answer *used*
it and whether the citation a reader clicks actually supports the claim.

* **Citation accuracy** - the answer cites the document that contains the gold evidence.
* **Citation validity** - every cited reference was among the sources actually retrieved
  (a citation the pipeline invented would fail here).
* **Fact coverage** - the numbers and key terms of the gold evidence appear in the answer.
  Lexical and approximate: it under-credits paraphrase, so treat it as a floor. An
  LLM-as-judge score per claim is the day-5 extension.
* **Abstention** - for questions whose answer is not in the caller's approved knowledge, the
  assistant must say so and cite nothing.

Rates come with 95% Wilson intervals; with a gold set this size, a single case moves a rate
by ~0.03, so overlapping intervals mean "not distinguishable", not "equal".

Usage: python -m evaluation.answer_eval [--api http://127.0.0.1:8000] [--model ollama/llama3.2-3b]
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from evaluation.chunking_eval import ROOT, _norm, wilson
from evaluation.client import post
from opsassist.providers.mock import _STOPWORDS

# Questions whose answer is NOT in the caller's approved knowledge: the assistant must
# abstain rather than answer from general knowledge or from another department's documents.
ABSTAIN_CASES = [
    ("U001", "What is the company parental leave policy?"),
    ("U001", "How many days of annual leave do employees get?"),  # HR-only
    ("U001", "Summarize the compensation review notes."),  # HR-confidential
    ("U003", "When may we deploy to production?"),  # Engineering-only
    ("U006", "What caused the August payment incident?"),  # Engineering-only
    ("U001", "What is our stock price target for next year?"),
]


@dataclass
class Scores:
    total: int = 0
    citation_correct: int = 0
    citation_valid: int = 0
    fact_coverage: float = 0.0
    fully_covered: int = 0
    wrongly_abstained: int = 0
    failures: list[str] = field(default_factory=list)


def key_terms(evidence: str) -> list[str]:
    """Numbers carry the fact; when there are none, fall back to content words."""
    numbers = re.findall(r"\d+(?:[.:,]\d+)*%?", evidence)
    if numbers:
        return numbers
    return [
        w for w in re.findall(r"[a-z]+", evidence.lower()) if w not in _STOPWORDS and len(w) > 3
    ]


def evaluate_answerable(
    api: httpx.Client, token: str, case: dict[str, Any], model: str | None
) -> tuple[Scores, dict[str, Any]]:
    scores = Scores(total=1)
    payload: dict[str, Any] = {"message": case["query"], "max_tokens": 500}
    if model:
        payload["model"] = model
    started = time.time()
    body = post(
        api, "/api/chat", json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=300
    ).json()
    answer = _norm(body.get("content", "")).replace(" %", "%")
    cited = {c["doc_key"] for c in body.get("citations", [])}
    retrieved = {
        h["doc_key"]
        for h in post(
            api,
            "/api/search",
            json={"query": case["query"]},
            headers={"Authorization": f"Bearer {token}"},
        ).json()["hits"]
    }

    if body.get("abstained"):
        scores.wrongly_abstained = 1
        scores.failures.append(f"{case['id']}: abstained although the document is readable")
        return scores, body
    scores.citation_correct = int(case["expected_doc"] in cited)
    scores.citation_valid = int(bool(cited) and cited <= retrieved)
    terms = [t for e in case["evidence"] for t in key_terms(e)]
    covered = sum(t.lower() in answer for t in terms) / len(terms) if terms else 0.0
    scores.fact_coverage = covered
    scores.fully_covered = int(covered == 1.0)
    if not scores.citation_correct:
        scores.failures.append(
            f"{case['id']}: cited {sorted(cited) or 'nothing'}, expected {case['expected_doc']}"
        )
    elif covered < 1.0:
        missing = [t for t in terms if t.lower() not in answer]
        scores.failures.append(f"{case['id']}: missing {missing} ({time.time() - started:.0f}s)")
    return scores, body


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--model", default=None, help="answer model; default = the catalog route")
    parser.add_argument("--limit", type=int, default=0, help="first N answerable cases only")
    args = parser.parse_args()

    cases = [json.loads(x) for x in (ROOT / "evaluation/retrieval_cases.jsonl").open()]
    if args.limit:
        cases = cases[: args.limit]
    tokens: dict[str, str] = {}
    totals = Scores()
    with httpx.Client(base_url=args.api, timeout=300) as api:

        def token(user: str) -> str:
            if user not in tokens:
                tokens[user] = post(api, "/api/auth/dev-token", json={"user_id": user}).json()[
                    "access_token"
                ]
            return tokens[user]

        for case in cases:
            scores, _ = evaluate_answerable(api, token(case["user"]), case, args.model)
            totals.total += scores.total
            totals.citation_correct += scores.citation_correct
            totals.citation_valid += scores.citation_valid
            totals.fact_coverage += scores.fact_coverage
            totals.fully_covered += scores.fully_covered
            totals.wrongly_abstained += scores.wrongly_abstained
            totals.failures += scores.failures

        abstained_correctly = 0
        abstain_failures = []
        for user, question in ABSTAIN_CASES:
            payload: dict[str, Any] = {"message": question, "max_tokens": 300}
            if args.model:
                payload["model"] = args.model
            body = post(
                api, "/api/chat", json=payload, headers={"Authorization": f"Bearer {token(user)}"}
            ).json()
            ok = bool(body.get("abstained")) and not body.get("citations")
            abstained_correctly += ok
            if not ok:
                abstain_failures.append(
                    f"{user}: {question[:50]} -> {body.get('content', '')[:80]}"
                )

    n, m = totals.total, len(ABSTAIN_CASES)

    def line(label: str, count: float, size: int) -> str:
        low, high = wilson(count, size)
        rate = count / size
        return f"{label:<22} {count:>5.1f}/{size:<3} = {rate:.3f}   95% CI {low:.2f}-{high:.2f}"

    print(f"\nAnswerable cases ({n}) - model: {args.model or 'catalog default'}")
    print(line("citation accuracy", totals.citation_correct, n))
    print(line("citation validity", totals.citation_valid, n))
    print(line("all facts present", totals.fully_covered, n))
    print(f"{'mean fact coverage':<22} {totals.fact_coverage / n:.3f}")
    print(line("wrongly abstained", totals.wrongly_abstained, n))
    print(f"\nMust-abstain cases ({m})")
    print(line("abstained correctly", abstained_correctly, m))
    if totals.failures or abstain_failures:
        print("\nFailures and partials:")
        for f in totals.failures + abstain_failures:
            print(f"  {f}")
    print(
        "\nOne case is ~"
        f"{1 / n:.3f} on the answerable set and ~{1 / m:.3f} on the abstain set; "
        "read the intervals, not the third decimal."
    )


if __name__ == "__main__":
    main()
