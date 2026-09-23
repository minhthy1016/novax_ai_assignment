# D-50: Evaluation design — deterministic where the answer is a rule, judged where it is prose

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

**Context.** The brief asks for at least 30 cases across seven kinds of behaviour and for
evidence on seven axes, from answer correctness to cost. The hard part is not running the
cases; it is making the resulting numbers mean something a reviewer can check.

**Decision.**

**1. Two graders, and the split is not negotiable.** Whether a tool call was authorized,
whether a pending action executed, whether a forbidden document appeared in the results —
these are rules, and they are compared against the case file exactly, with no model
involved. Whether an answer conveys a fact, or asserts something its sources do not support,
is prose, and that is the only thing the LLM judge grades. A grader that can be argued with
has no business deciding a policy question.

**2. The judge sits outside the system under test.** It calls the provider directly, never
`/api/chat`, so a retrieval bug cannot help the grader agree with the answer it produced.

**3. A different model family than the answer.** Answers come from Llama, judging from Qwen;
the runner refuses a same-family pair unless explicitly overridden. A model marking its own
homework measures self-consistency. The judge is also **local**, which means judging a
confidential answer never sends it off the machine — the same rule the assistant follows
(D-15). This is why the judge is not simply "the best available API model".

**4. One verdict per claim, with the evidence attached.** Each reference fact is judged
alone — `supported`, `contradicted` or `missing` — against the answer *and* the passages the
pipeline retrieved. A single "score this answer 1-5" hides which fact was dropped; a per-claim
verdict names it. The judge also returns the statements it could not trace to any passage,
which is the hallucination signal, and every one is printed in the report so a human can
overrule the judge.

**5. String guards where a failure has a shape.** `must_not_contain` catches the specific
wrong answers that matter: another department's number appearing in a refusal, "created
successfully" in a case that must stay pending, "14 days" leaking to Engineering. These cost
nothing and cannot drift, unlike a judge's opinion.

**6. A control that is allowed to look good.** The same class of model answers the same
questions with no retrieval, no scope and no citations. It states plenty of facts. The point
of the control is not that it is wrong — it is that nothing about it can be checked, and it
answers questions the caller was never entitled to ask.

**7. Cases carry a named actor.** Every case is one employee asking one question, so
isolation, authorization and retrieval scope are measured on the same run as answer quality,
rather than in a separate security test that a reader has to correlate by hand.

**8. Reported with intervals, and with the failures quoted.** Every rate carries a 95%
Wilson interval; with 71 cases one case moves a rate by ~0.014 and a per-category rate by far
more. Each failing case is printed with its checks and its answer, so the report can be
audited rather than believed.

**Consequences.** A full run needs a second local model (`ollama pull qwen2.5:7b`), takes
tens of minutes and is not part of CI; `make eval-fast` runs the deterministic axes in a few
minutes with no judge, which is what a pull request gets. Judge verdicts vary slightly run to
run — that variance is visible in the report rather than hidden, and the deterministic axes
do not move at all.

**Alternatives considered.** RAGAS and similar frameworks (faithfulness / answer-relevancy
metrics that are themselves LLM calls with prompts we would not control, and no notion of
*who is asking* — the axis this system exists to enforce); a single overall judge score per
answer (cheaper, but it cannot say which fact was missed and tends to reward fluency); exact
string matching only (what the earlier `answer_eval.py` did: it under-credits paraphrase, so
it was kept as a fast lexical floor rather than the headline); a hosted frontier model as
judge (better judgement, but it cannot see confidential answers and makes the score depend on
a vendor and a bill).

**Tested by.** `tests/unit/test_eval_harness.py` — the grading logic is unit-tested against
hand-built responses, including a sensitive action that executed when it should have been
pending, a dead provider that produced prose, and a judge reply that cannot be parsed.
