"""The evaluation suite: 62 cases through the running API, scored on seven axes.

Every case is one request from one named employee, so what is measured is what a reviewer
would see: routing, authorization, retrieval, citations, abstention, cost and latency.

What is scored, and by what:

| Axis | Scored by |
|---|---|
| Tool accuracy | the case's `expected_tool` / `expected_arguments` / outcome, deterministically |
| Retrieval relevance | rank of `expected_sources` in `/api/search` for that caller (MRR) |
| Isolation | `forbidden_sources` must appear in neither the hits nor the citations |
| Citation correctness | every cited source was retrieved, and the expected source is cited |
| Abstention | the outcome the case requires, deterministically |
| Answer correctness | an LLM judge, one verdict per reference fact (`judge.py`) |
| Hallucination | the judge's unsupported claims, plus `must_not_contain` string guards |
| Performance and cost | end-to-end latency, provider latency, tokens and cost per case |

The deterministic axes never ask a model anything: a policy decision is right or wrong, and
a grader that can be argued with has no place in it. The judge only grades prose.

Usage:
  python -m evaluation.run_eval                          # full run, local judge
  python -m evaluation.run_eval --categories tool_selection,confirmation
  python -m evaluation.run_eval --no-judge               # deterministic axes only (fast)
  python -m evaluation.run_eval --control                # also run the ungrounded baseline
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from evaluation.chunking_eval import ROOT, wilson
from evaluation.client import get, post
from evaluation.judge import Judge, family_of

CASES = ROOT / "evaluation/cases.jsonl"
REPORTS = ROOT / "evaluation/reports"
RUNS = ROOT / "evaluation/runs"


@dataclass
class CaseResult:
    case_id: str
    category: str
    prompt: str
    actor: str
    # Deterministic checks: name -> (passed, detail). A check that does not apply is absent.
    checks: dict[str, tuple[bool, str]] = field(default_factory=dict)
    facts: list[dict[str, Any]] = field(default_factory=list)
    unsupported_claims: list[str] = field(default_factory=list)
    citation_support: list[dict[str, Any]] = field(default_factory=list)
    rank_of_expected: int | None = None
    latency_ms: float = 0.0
    model_latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    route: str = ""
    answer: str = ""
    error: str | None = None

    @property
    def passed(self) -> bool:
        return all(ok for ok, _ in self.checks.values()) and not self.failed_facts

    @property
    def failed_facts(self) -> list[dict[str, Any]]:
        # A verdict the judge could not evidence is not a failure of the system under test.
        return [
            f
            for f in self.facts
            if f.get("verdict") in ("missing", "contradicted") and not f.get("unverified")
        ]

    def failures(self) -> list[str]:
        out = [f"{name}: {detail}" for name, (ok, detail) in self.checks.items() if not ok]
        out += [
            f"fact {f['verdict']}: {f['fact']}"
            + (f" (judge quoted: {f['quote'][:70]!r})" if f.get("quote") else "")
            for f in self.failed_facts
        ]
        return out


# ------------------------------------------------------------------ the run


def token_for(api: httpx.Client, user: str, cache: dict[str, str]) -> str:
    if user not in cache:
        issued = post(api, "/api/auth/dev-token", json={"user_id": user}).json()
        cache[user] = issued["access_token"]
    return cache[user]


def outcome_checks(case: dict[str, Any], body: dict[str, Any]) -> dict[str, tuple[bool, str]]:
    """Every policy-shaped expectation, decided without a model."""
    checks: dict[str, tuple[bool, str]] = {}
    expected = case["expected_outcome"]
    tool = body.get("tool") or {}
    route, status = body.get("route"), tool.get("status")
    abstained = bool(body.get("abstained"))
    attempts = body.get("attempts") or []

    match expected:
        case "answer":
            checks["answered"] = (not abstained and bool(body.get("content")), f"route={route}")
        case "abstain":
            checks["abstained"] = (abstained, f"route={route}, abstained={abstained}")
        case "answer_or_abstain":
            checks["no_invention"] = (route in ("knowledge", "small_talk"), f"route={route}")
        case "tool_ok" | "tool_denied" | "tool_pending":
            want = expected.removeprefix("tool_")
            checks["tool_status"] = (status == want, f"status={status!r}, wanted {want!r}")
        case "tool_any":
            checks["tool_called"] = (bool(tool), f"route={route}")
        case "no_tool":
            checks["no_tool_call"] = (not tool, f"tool={tool.get('name')}")
        case "no_execution":
            checks["nothing_executed"] = (status != "ok", f"status={status!r}")
        case "refuse":
            checks["refused"] = (route == "refuse", f"route={route}")
        case "answer_after_fallback":
            used = body.get("fallback_used")
            checks["fallback_recovered"] = (
                bool(used) and not abstained,
                f"fallback_used={used}, attempts={len(attempts)}",
            )
        case "controlled_failure":
            # A dead provider must produce a clean failure, never an invented answer.
            checks["failed_cleanly"] = (
                abstained or not body.get("content"),
                f"content={str(body.get('content'))[:60]!r}",
            )

    if case.get("expected_tool"):
        checks["tool_choice"] = (
            tool.get("name") == case["expected_tool"],
            f"chose {tool.get('name')!r}, wanted {case['expected_tool']!r}",
        )
    elif case.get("expected_tool") is None and expected.startswith(("answer", "abstain")):
        checks["no_stray_tool"] = (not tool, f"tool={tool.get('name')}")

    if wanted_args := case.get("expected_arguments"):
        got = (tool.get("data") or {}) | (body.get("tool_arguments") or {})
        matched = all(str(got.get(k, "")).lower() == str(v).lower() for k, v in wanted_args.items())
        checks["tool_arguments"] = (matched, f"wanted {wanted_args}, got {got or '{}'}")

    for phrase in case.get("must_not_contain", []):
        held = phrase.lower() not in str(body.get("content", "")).lower()
        checks[f"absent:{phrase[:28]}"] = (held, "absent" if held else "appeared in the answer")
    return checks


def _claim_for(answer: str, number: int) -> str:
    """The sentence a citation marker sits in - that is the claim it is being made about."""
    for sentence in re.split(r"(?<=[.!?])\s+", answer):
        if f"[{number}]" in sentence or f"#{number}" in sentence:
            return re.sub(r"\[\d+\]|#\d+", "", sentence).strip()
    return ""


def run_case(
    api: httpx.Client, case: dict[str, Any], tokens: dict[str, str], judge: Judge | None
) -> CaseResult:
    result = CaseResult(
        case_id=case["case_id"],
        category=case["category"],
        prompt=case["prompt"],
        actor=case["actor_id"],
    )
    headers = {"Authorization": f"Bearer {token_for(api, case['actor_id'], tokens)}"}
    payload: dict[str, Any] = {"message": case["prompt"], "max_tokens": 600}
    if model := case.get("model"):
        payload["model"] = model

    started = time.perf_counter()
    response = post(api, "/api/chat", json=payload, headers=headers, timeout=300)
    result.latency_ms = (time.perf_counter() - started) * 1000
    if response.status_code != 200:
        body = response.json()
        # A provider-failure case may legitimately end as a controlled HTTP error.
        if case["expected_outcome"] == "controlled_failure":
            result.checks["failed_cleanly"] = (
                response.status_code in (502, 503, 504),
                f"status={response.status_code}",
            )
            return result
        result.error = f"HTTP {response.status_code}: {body.get('error', {}).get('code')}"
        result.checks["request"] = (False, result.error)
        return result

    body = response.json()
    result.route = str(body.get("route"))
    result.answer = str(body.get("content", ""))
    usage = body.get("usage") or {}
    result.prompt_tokens = int(usage.get("prompt_tokens", 0))
    result.completion_tokens = int(usage.get("completion_tokens", 0))
    result.cost_usd = float(usage.get("cost_usd", 0) or 0)
    result.model_latency_ms = sum(float(a.get("latency_ms", 0)) for a in body.get("attempts") or [])
    result.checks = outcome_checks(case, body)

    # ---- retrieval, from the same caller's scope
    hits: list[dict[str, Any]] = []
    if case.get("expected_sources") or case.get("forbidden_sources"):
        hits = post(
            api, "/api/search", json={"query": case["prompt"], "top_k": 4}, headers=headers
        ).json()["hits"]
        keys = [h["doc_key"] for h in hits]
        if wanted := case.get("expected_sources"):
            rank = next((i for i, k in enumerate(keys, 1) if k in wanted), None)
            result.rank_of_expected = rank
            result.checks["retrieved_expected"] = (rank is not None, f"top-4 = {keys}")
        for forbidden in case.get("forbidden_sources", []):
            result.checks[f"isolated:{forbidden}"] = (
                forbidden not in keys,
                f"retrievable by {case['actor_id']}",
            )

    # ---- citations
    cited = {c["doc_key"] for c in body.get("citations") or []}
    if cited:
        retrieved = {h["doc_key"] for h in hits} if hits else cited
        result.checks["citations_valid"] = (
            cited <= retrieved,
            f"cited {sorted(cited)}, retrieved {sorted(retrieved)}",
        )
    # An answer (not an abstention) to a question with an expected source must cite it; an
    # uncited answer used to skip this check entirely and pass.
    wanted = case.get("expected_sources")
    if wanted and (cited or (body.get("content") and not body.get("abstained"))):
        result.checks["cited_expected"] = (
            bool(cited & set(wanted)),
            f"cited {sorted(cited) or 'nothing'}, wanted one of {wanted}",
        )
    for forbidden in case.get("forbidden_sources", []):
        result.checks[f"not_cited:{forbidden}"] = (
            forbidden not in cited,
            "cited a forbidden source",
        )

    # ---- the judge, on prose only
    # Judge only what the pipeline itself wrote from sources. A tool message, a denial and a
    # mock provider's echo are rendered by the server or by a fixture: grading them as prose
    # measures the fixture, and every "unsupported claim" it produced in the first run was one
    # of those. `judged` says so explicitly rather than leaving it to a route check.
    judged = (
        judge is not None
        and result.route == "knowledge"
        and not str(case.get("model", "")).startswith(("mock/", "demo-"))
    )
    if judged and judge is not None:
        passages = [h["context"] for h in hits] or [
            c.get("snippet", "") for c in body.get("citations") or []
        ]
        for fact in case.get("reference_facts", []):
            judgement = judge.judge_fact(case["prompt"], fact, result.answer, passages)
            result.facts.append(
                {
                    "fact": fact,
                    "verdict": judgement.verdict,
                    "quote": judgement.quote,
                    "note": judgement.note,
                    "error": judgement.error,
                    "unverified": judgement.unverified,
                }
            )
            result.unsupported_claims += judgement.unsupported
        # "A citation supports the exact claim": take the sentence each marker is attached
        # to and ask whether the passage behind that marker really says it. Checking that the
        # document was retrieved (above) is a weaker claim than checking that the passage a
        # reader would open contains the statement.
        # Judge against the whole section the citation resolves to, not the 300-character
        # snippet the API returns for display: a reader who follows `[1]` lands on the
        # section. Judging the snippet alone marked correct citations wrong simply because
        # the sentence fell outside the excerpt.
        # Key by the full reference, not by document: a citation points at one section, and
        # keying by doc_key handed the judge whichever section of that document happened to
        # rank first - which is how four correct citations were first marked unsupported.
        sections = {h["ref"]: h["context"] for h in hits}
        for citation in body.get("citations") or []:
            claim = _claim_for(result.answer, int(citation["number"]))
            passage = sections.get(citation["ref"]) or str(citation.get("snippet", ""))
            if not claim or not passage:
                continue
            supports = judge.judge_citation(claim, passage)
            result.citation_support.append(
                {"ref": citation["ref"], "claim": claim[:160], "supports": supports}
            )
        if not case.get("reference_facts") and result.answer and not body.get("abstained"):
            judgement = judge.judge_grounding(case["prompt"], result.answer, passages)
            result.unsupported_claims += judgement.unsupported
    return result


# ------------------------------------------------------------------ reporting


def rate(count: float, total: int) -> str:
    if total == 0:
        return "n/a"
    low, high = wilson(count, total)
    return f"{count:.0f}/{total} = {count / total:.3f} (95% CI {low:.2f}-{high:.2f})"


def summarize(results: list[CaseResult], meta: dict[str, Any]) -> str:
    total = len(results)
    passed = sum(r.passed for r in results)
    by_category: dict[str, list[CaseResult]] = defaultdict(list)
    for r in results:
        by_category[r.category].append(r)

    def axis(name: str) -> tuple[float, int]:
        """One count per check: a case with two guards contributes two, each scored alone."""
        verdicts = [v for r in results for k, (v, _) in r.checks.items() if k.startswith(name)]
        return sum(verdicts), len(verdicts)

    facts = [f for r in results for f in r.facts]
    supported = sum(f["verdict"] == "supported" for f in facts)
    contradicted = sum(f["verdict"] == "contradicted" for f in facts)
    unverified = sum(bool(f.get("unverified")) for f in facts)
    judge_errors = sum(bool(f.get("error")) and not f.get("unverified") for f in facts)
    unsupported = [(r.case_id, c) for r in results for c in r.unsupported_claims]
    citations = [c for r in results for c in r.citation_support if c["supports"] is not None]
    supporting = sum(c["supports"] for c in citations)
    bad_citations = [
        (r.case_id, c) for r in results for c in r.citation_support if c["supports"] is False
    ]
    ranks = [r.rank_of_expected for r in results if r.rank_of_expected]
    expected_retrieval = [r for r in results if "retrieved_expected" in r.checks]
    mrr = sum(1 / r for r in ranks) / len(expected_retrieval) if expected_retrieval else 0.0
    latencies = sorted(r.latency_ms for r in results if not r.error)
    provider_s = statistics.mean([r.model_latency_ms for r in results]) / 1000
    prompt_total = sum(r.prompt_tokens for r in results)
    completion_total = sum(r.completion_tokens for r in results)
    mean_tokens = statistics.mean([r.prompt_tokens + r.completion_tokens for r in results])
    isolation_failures = [
        (r.case_id, k)
        for r in results
        for k, (ok, _) in r.checks.items()
        if not ok and (k.startswith("isolated:") or k.startswith("not_cited:"))
    ]

    def pct(values: list[float], q: float) -> float:
        return values[min(int(len(values) * q), len(values) - 1)] if values else 0.0

    lines = [
        f"# Evaluation run — {meta['started']}",
        "",
        f"* answering model: `{meta['answer_model']}` · judge: `{meta['judge_model']}`"
        f" ({meta['judge_family']} vs {meta['answer_family']})",
        f"* cases: **{total}** across {len(by_category)} categories · "
        f"passed every deterministic check and fact: **{passed}/{total}**",
        f"* wall clock: {meta['duration_s']:.0f}s",
        "",
        "## Headline",
        "",
        "| Axis | Result |",
        "|---|---|",
        f"| Cases fully correct | {rate(passed, total)} |",
        f"| Tool accuracy (choice, arguments, status) | {rate(*axis('tool_'))} |",
        f"| Abstention / refusal behaviour | {rate(*axis('abstain'))} |",
        f"| Retrieval: expected source in top-4 | {rate(*axis('retrieved_expected'))} |",
        f"| Retrieval MRR (expected source) | {mrr:.3f} |",
        f"| Citations valid (cited ⊆ retrieved) | {rate(*axis('citations_valid'))} |",
        f"| Expected source cited | {rate(*axis('cited_expected'))} |",
        f"| Citation supports the exact claim (judged, per citation) |"
        f" {rate(supporting, len(citations))} |",
        f"| Department isolation held | {rate(*axis('isolated'))} |",
        f"| Hallucination guards (`must_not_contain`) | {rate(*axis('absent:'))} |",
        f"| Judge: reference facts supported | {rate(supported, len(facts))} |",
        f"| Judge: facts contradicted | {contradicted} |",
        f"| Judge: unsupported claims found | {len(unsupported)} |",
        f"| Judge verdicts discarded (could not quote the answer) | {unverified} |",
        "",
        "## Cost and latency",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| End-to-end p50 / p95 | {pct(latencies, 0.5) / 1000:.2f}s /"
        f" {pct(latencies, 0.95) / 1000:.2f}s |",
        f"| Provider time, mean per case | {provider_s:.2f}s |",
        f"| Tokens, total prompt → completion | {prompt_total} → {completion_total} |",
        f"| Tokens, mean per case | {mean_tokens:.0f} |",
        f"| Estimated cost, whole suite | ${sum(r.cost_usd for r in results):.4f} |",
        "",
        "## By category",
        "",
        "| Category | Cases | Fully correct | Mean latency | Mean tokens |",
        "|---|---:|---|---:|---:|",
    ]
    for name, group in sorted(by_category.items()):
        ok = sum(r.passed for r in group)
        lines.append(
            f"| {name} | {len(group)} | {ok}/{len(group)} | "
            f"{statistics.mean([r.latency_ms for r in group]) / 1000:.1f}s | "
            f"{statistics.mean([r.prompt_tokens + r.completion_tokens for r in group]):.0f} |"
        )

    lines += ["", "## Critical findings", ""]
    lines.append(
        "None: no forbidden source was retrieved or cited in any case."
        if not isolation_failures
        else "\n".join(f"* **{cid}**: {k}" for cid, k in isolation_failures)
    )

    failed = [r for r in results if not r.passed]
    lines += ["", f"## Failures and partials ({len(failed)})", ""]
    for r in failed:
        lines.append(f"**{r.case_id}** ({r.category}, {r.actor}) — {r.prompt}")
        lines += [f"  * {f}" for f in r.failures()]
        if r.answer:
            lines.append(f"  * answered: {r.answer[:180].strip()}")
        lines.append("")
    if bad_citations:
        lines += ["## Citations that did not support their claim", ""]
        lines += [f"* **{cid}** {c['ref']}: {c['claim']}" for cid, c in bad_citations]
        lines.append("")
    if unsupported:
        lines += ["## Claims the judge could not trace to a source", ""]
        lines += [f"* **{cid}**: {claim[:160]}" for cid, claim in unsupported]
        lines.append("")
    if judge_errors:
        lines += [f"> The judge failed to return a usable verdict {judge_errors} time(s).", ""]
    lines += [
        "## Reading these numbers",
        "",
        f"With {total} cases a single case moves a rate by ~{1 / total:.3f}, and the per-category"
        " counts are smaller still, so the intervals matter more than the third decimal."
        " The deterministic axes (tool choice, authorization, isolation, abstention) are exact:"
        " they compare against the policy the case states, not against a model's opinion."
        " Only the fact verdicts and unsupported claims come from the judge"
        f" (a {meta['judge_family']} model), and it must quote the answer to justify a verdict:"
        f" {unverified} verdict(s) in this run could not be quoted and were discarded rather"
        " than counted against the system. Every remaining failure prints the answer, so the"
        " judge itself can be overruled by a reader.",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ control


def run_control(judge: Judge, cases: list[dict[str, Any]]) -> str:
    """What the same question looks like with no retrieval, no scope and no citations.

    The point is not that the control is bad - it is that it cannot be checked. It answers
    questions it has no source for, including the ones a caller is not allowed to ask.
    """
    graded = [c for c in cases if c.get("reference_facts") or c.get("must_not_contain")]
    supported = invented = guarded = 0
    guards = 0
    for case in graded:
        answer = judge.answer_without_retrieval(case["prompt"])
        for fact in case.get("reference_facts", []):
            judgement = judge.judge_fact(case["prompt"], fact, answer, [])
            supported += judgement.verdict == "supported"
        for phrase in case.get("must_not_contain", []):
            guards += 1
            guarded += phrase.lower() not in answer.lower()
        if case["expected_outcome"] == "abstain" and len(answer) > 120:
            invented += 1
    facts = sum(len(c.get("reference_facts", [])) for c in graded)
    abstain_cases = [c for c in graded if c["expected_outcome"] == "abstain"]
    return "\n".join(
        [
            "## Control: the same model with no retrieval and no policy",
            "",
            "| Measure | Ungrounded control | Meaning |",
            "|---|---|---|",
            f"| Reference facts stated | {rate(supported, facts)} |"
            " from memory, with no source to check |",
            f"| Hallucination guards held | {rate(guarded, guards)} |"
            " forbidden phrasings it produced anyway |",
            f"| Questions it should not answer, answered anyway | {invented}/{len(abstain_cases)} |"
            " it has no notion of who is asking |",
            "| Citations | none | nothing to verify an answer against |",
            "",
        ]
    )


# ------------------------------------------------------------------ main


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--model", default=None, help="answering model; default = catalog route")
    parser.add_argument("--judge", default="ollama/qwen2.5:7b", help="judge model, or 'none'")
    parser.add_argument("--categories", default="", help="comma-separated subset")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--control", action="store_true", help="also run the ungrounded baseline")
    parser.add_argument("--allow-same-family", action="store_true")
    args = parser.parse_args()

    cases = [json.loads(line) for line in CASES.open() if line.strip()]
    if args.categories:
        wanted = {c.strip() for c in args.categories.split(",")}
        cases = [c for c in cases if c["category"] in wanted]
    if args.limit:
        cases = cases[: args.limit]

    judge = None if (args.no_judge or args.judge == "none") else Judge(args.judge)
    answer_model = args.model or "catalog default (ollama/llama3.2-3b)"
    if judge and family_of(args.judge) == family_of(answer_model) and not args.allow_same_family:
        raise SystemExit(
            f"judge {args.judge!r} and answering model {answer_model!r} are the same family; "
            "a model marking its own homework measures agreement, not correctness "
            "(--allow-same-family to override)"
        )

    started = time.perf_counter()
    tokens: dict[str, str] = {}
    results: list[CaseResult] = []
    with httpx.Client(base_url=args.api, timeout=300) as api:
        get(api, "/readyz")
        for index, case in enumerate(cases, 1):
            if args.model:
                case = case | {"model": case.get("model", args.model)}
            result = run_case(api, case, tokens, judge)
            results.append(result)
            mark = "ok  " if result.passed else "FAIL"
            print(f"[{index:>2}/{len(cases)}] {mark} {result.case_id:<4} {case['prompt'][:56]}")

    meta = {
        "started": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "answer_model": answer_model,
        "judge_model": args.judge if judge else "none",
        "answer_family": family_of(answer_model),
        "judge_family": family_of(args.judge) if judge else "none",
        "duration_s": time.perf_counter() - started,
    }
    report = summarize(results, meta)
    if args.control and judge:
        report += "\n\n" + run_control(judge, cases)
    if judge:
        judge.close()

    REPORTS.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    (REPORTS / "evaluation.md").write_text(report)
    (RUNS / f"eval-{stamp}.json").write_text(
        json.dumps({"meta": meta, "results": [vars(r) for r in results]}, indent=1, default=str)
    )
    print("\n" + report)
    print(f"\nWritten: evaluation/reports/evaluation.md and evaluation/runs/eval-{stamp}.json")


if __name__ == "__main__":
    main()
