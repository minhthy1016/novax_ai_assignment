"""The evaluation harness itself: its grading must be checkable, or the numbers are noise."""

from __future__ import annotations

import json

import pytest
from evaluation.judge import Judge, family_of
from evaluation.run_eval import CASES, outcome_checks

CATEGORIES = {
    "answerable",
    "unanswerable",
    "misleading_premise",
    "cross_department",
    "tool_selection",
    "confirmation",
    "provider_failure",
    "injection",
}


def cases() -> list[dict]:
    return [json.loads(line) for line in CASES.open() if line.strip()]


def test_the_suite_covers_every_category_the_brief_names() -> None:
    rows = cases()
    assert len(rows) >= 30
    assert {r["category"] for r in rows} == CATEGORIES
    assert len({r["case_id"] for r in rows}) == len(rows)
    for row in rows:
        assert row["actor_id"].startswith("U")
        assert row["prompt"].strip()
        assert row["expected_outcome"]


def test_a_correct_answer_passes_and_a_hallucinated_one_does_not() -> None:
    case = {
        "expected_outcome": "answer",
        "expected_sources": ["KB-ENG-001"],
        "expected_tool": None,
        "must_not_contain": ["Friday"],
    }
    good = outcome_checks(
        case, {"route": "knowledge", "content": "Tuesday or Thursday.", "tool": None}
    )
    assert all(ok for ok, _ in good.values())

    bad = outcome_checks(
        case, {"route": "knowledge", "content": "Deployments run on Friday.", "tool": None}
    )
    assert not bad["absent:Friday"][0]


def test_an_abstention_is_only_credited_when_the_system_actually_abstained() -> None:
    case = {"expected_outcome": "abstain", "expected_tool": None}
    assert outcome_checks(case, {"route": "knowledge", "abstained": True})["abstained"][0]
    answered = outcome_checks(
        case, {"route": "knowledge", "abstained": False, "content": "14 days"}
    )
    assert not answered["abstained"][0]


@pytest.mark.parametrize(
    ("expected", "status", "should_pass"),
    [
        ("tool_pending", "pending", True),
        ("tool_pending", "ok", False),  # a sensitive action that executed straight away
        ("tool_denied", "denied", True),
        ("tool_denied", "ok", False),
        ("tool_ok", "ok", True),
    ],
)
def test_sensitive_and_denied_outcomes_are_graded_exactly(
    expected: str, status: str, should_pass: bool
) -> None:
    case = {"expected_outcome": expected, "expected_tool": "create_vpn_profile"}
    body = {"route": "tool", "tool": {"name": "create_vpn_profile", "status": status}}
    assert outcome_checks(case, body)["tool_status"][0] is should_pass


def test_a_stray_tool_call_fails_a_knowledge_case() -> None:
    case = {"expected_outcome": "answer", "expected_tool": None}
    body = {
        "route": "tool",
        "content": "done",
        "tool": {"name": "create_support_ticket", "status": "ok"},
    }
    assert not outcome_checks(case, body)["no_stray_tool"][0]


def test_a_dead_provider_must_not_produce_prose() -> None:
    case = {"expected_outcome": "controlled_failure", "expected_tool": None}
    silent = outcome_checks(case, {"route": "knowledge", "content": "", "abstained": True})
    assert silent["failed_cleanly"][0]
    invented = outcome_checks(case, {"route": "knowledge", "content": "The window is Tuesday."})
    assert not invented["failed_cleanly"][0]


@pytest.mark.security
def test_the_judge_must_not_be_the_same_family_as_the_answer() -> None:
    """A model grading its own family measures agreement, not correctness."""
    assert family_of("ollama/qwen2.5:7b") != family_of("ollama/llama3.2-3b")
    assert family_of("nim/gpt-oss-20b") != family_of("ollama/llama3.2-3b")
    assert family_of("ollama/llama3.2-3b") == family_of("nim/llama-3.3-70b-instruct")


def test_the_judge_reports_an_unusable_reply_instead_of_guessing() -> None:
    judge = Judge("ollama/qwen2.5:7b")
    judge._complete = lambda *a, **k: "I think it is probably fine"  # type: ignore[method-assign]
    verdict = judge.judge_fact("q", "fact", "answer", [])
    assert verdict.verdict is None and verdict.error
    judge._complete = lambda *a, **k: '{"verdict": "excellent"}'  # type: ignore[method-assign]
    assert judge.judge_fact("q", "fact", "answer", []).error
    judge.close()


def test_a_verdict_the_judge_cannot_quote_is_discarded() -> None:
    """The judge marked a correct answer 'contradicted' by reading the reference fact as if
    it were the answer. A verdict must now be evidenced by words that are really there."""
    judge = Judge("ollama/qwen2.5:7b")
    answer = "Employees receive 14 days of annual leave after confirmation."

    judge._complete = lambda *a, **k: json.dumps(  # type: ignore[method-assign]
        {"verdict": "contradicted", "quote": "employees receive 21 days", "note": "n"}
    )
    bad = judge.judge_fact("q", "Employees receive 14 days", answer, [])
    assert bad.unverified and bad.verdict is None  # not in the answer: discarded

    judge._complete = lambda *a, **k: json.dumps(  # type: ignore[method-assign]
        {"verdict": "supported", "quote": "receive 14 days of annual leave", "note": "n"}
    )
    good = judge.judge_fact("q", "Employees receive 14 days", answer, [])
    assert good.verdict == "supported" and not good.unverified

    # "supported" is not held to the quote rule: recognising a paraphrase is the judge's job.
    judge._complete = lambda *a, **k: json.dumps(  # type: ignore[method-assign]
        {"verdict": "supported", "quote": "(paraphrased)", "note": "n"}
    )
    assert judge.judge_fact("q", "14 days", "Staff get two weeks plus.", []).verdict == "supported"
    judge.close()


def test_grounding_drops_claims_the_passages_visibly_contain() -> None:
    from evaluation.judge import _found_in

    passages = ["SEV2: update the status page every 60 minutes."]
    assert _found_in("The status page is updated every 60 minutes for SEV2 incidents", passages)
    # A changed number, a new entity or an unrelated statement is kept for the report.
    assert not _found_in("The status page is updated every 30 minutes for SEV2", passages)
    assert not _found_in("The status page is updated every 60 minutes for SEV1", passages)
    assert not _found_in("Deployments are allowed on Fridays", passages)


def test_a_contradiction_whose_quote_states_the_fact_is_discarded(monkeypatch) -> None:
    from evaluation import judge as judge_module

    judge = judge_module.Judge("ollama/qwen2.5:7b")
    answer = "No, according to the policy [1], employees receive 14 days of annual leave."
    reply = (
        '{"verdict": "contradicted", "quote": "employees receive 14 days of annual leave", '
        '"note": "x"}'
    )
    monkeypatch.setattr(judge, "_complete", lambda *_: reply)
    verdict = judge.judge_fact("21 days, right?", "Employees receive 14 days of annual leave",
                               answer, [])  # fmt: skip
    assert verdict.verdict is None and verdict.unverified
