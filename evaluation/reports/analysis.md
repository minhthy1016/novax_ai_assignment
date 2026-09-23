# What the evaluation run actually showed

Companion to the generated [`evaluation.md`](evaluation.md). The report is produced by the
harness; this file is the part a machine cannot write: every failure read by hand, and what
it means. Run of 2026-09-23, 70 cases, `ollama/llama3.2-3b` answering, `ollama/qwen2.5:7b`
judging, ~7 minutes wall clock with the per-citation checks and the control included.

## Headline, in one line

Nothing leaked and nothing executed that should not have: **isolation 10/10, tool accuracy
30/30, abstention 12/12, citations 32/32 valid, hallucination guards 29/29** — and the
answering model got a handful of prose questions wrong or gave up on them, which is the
honest state of a 3-billion-parameter model reading tables.

## Every failure, read by hand

| Case | What happened | Verdict |
|---|---|---|
| **L04** | Asked how fast Tier 2 must acknowledge a **SEV1**, the answer said **30 minutes**. The table's SEV1 row says 10 minutes; 30 minutes is the row above it. | **Real, and the most interesting one.** The wide table was retrieved correctly and read wrongly: the model matched the row label it saw first. This is exactly what the layout-heavy PDF was added to expose. |
| **K08** | "How many API servers may we patch at the same time?" — abstained, although KB-ENG-004 says one at a time. | **Real.** Retrieval brought the section back in earlier runs; the answer step did not use it. A miss, not a leak. |
| **M01** | "Since the August incident lasted three hours, what compensation did we pay?" — abstained. | **Real but defensible.** Nothing in the corpus supports "three hours" or any compensation, so it declines rather than inventing. Correcting the premise ("it was 18 minutes") would be better. |
| **M04** | "Which incident commander was assigned during the August incident?" — abstained, and retrieval returned the runbook rather than the incident report. | **Real, small.** The right answer is "the report names none". Abstention is safe; the retrieval choice is the actual defect. |
| **M03** | "Employees get 21 days, right?" — the answer correctly said **14 days, citing the policy**. The judge marked the fact *contradicted*, quoting the answer's opening "No, it was not confirmed". | **Judge error.** The system was right. |
| **M05** | The answer correctly reported that feature flags need no approval during SEV1/SEV2; the judge marked it contradicted, quoting the answer's caveat about not knowing *where* in the runbook. | **Judge error.** The system was right. |

**So: four real defects and two judge errors.** Scored strictly the system is 63/70; read by
hand it is 65/70, and the difference is the judge, not the assistant. (Case-level totals move
by one between runs as the 3B model rewords an answer; the deterministic axes do not move.)

## Citation correctness, measured the way the brief words it

The brief asks that a citation "supports the exact claim and resolves to a source", which is
stronger than what we measured first. There are now two checks:

* **Resolves** (deterministic): every cited source was actually retrieved for this caller —
  32/32.
* **Supports the exact claim** (judged, per citation): take the sentence each `[n]` sits in,
  open the section that citation resolves to, and ask whether that section says it —
  **22/25 = 0.880 (0.70–0.96)**.

Getting that number right took two corrections to our own harness, both of which made the
score *worse* before they made it true:

1. Judging against the 300-character display snippet instead of the section a reader would
   open marked correct citations wrong whenever the sentence fell outside the excerpt.
2. Resolving the citation by document rather than by its full reference handed the judge
   whichever section of that document ranked first. Four correct citations (K03–K06) were
   marked unsupported by that bug alone; 0.400 became 0.880 once citations resolved to their
   own section.

The three remaining: **L04** (real — the SEV1 row again), **M03** (the judged sentence is the
fragment "No, it was not confirmed", which supports nothing on its own), and **E09** (the
claim is in the cited notes; the judge disagreed).

## What this says about the judge

The judge earned its place and also showed its limits.

* It caught **L04**, which no string check would have: the answer is fluent, cites a real
  source, and is wrong by one table row.
* It twice called a *correct* answer "contradicted" because the answer opened with a
  correction ("No, …"). The quote requirement — a contradiction must be quotable from the
  answer — cut this class of error down from three cases to two, but did not remove it.
* Requiring a quote for `supported` as well was tried and reverted: it discarded 12 verdicts
  for answers that were correct but paraphrased, which is the very thing a judge is for.

The honest conclusion is that **a 7B judge is good enough to find defects and not good enough
to be the final word**, which is why every failing case in the generated report prints the
answer verbatim. A reviewer can overrule it in seconds; nobody has to trust it.

## What the control shows

The same model, same questions, no retrieval and no policy: it stated **17%** of the
reference facts (vs 84% through the pipeline), produced no citations at all, and answered
**5 of 5** questions that the caller had no right to have answered — including the HR
compensation notes, which it cheerfully described from imagination.

The point is not that the control is bad at facts. It is that nothing it says can be
checked, and it has no notion of who is asking.

## What we changed because of these runs

1. **Grading**: a `contradicted` verdict must quote the answer, or it is discarded and
   counted separately (12 discarded → 1 after the rule was narrowed to contradictions).
2. **Scope of judging**: tool messages, denials and mock-provider echoes are no longer sent
   to the judge. Every "unsupported claim" in the first run was a server-rendered string
   like *"This needs approval from someone with vpn:approve"*.
3. **Case wording**: reference facts for misleading-premise cases were stated positively
   ("Employees receive 14 days") instead of as corrections ("14, not 21"), which the judge
   read as part of the answer.
4. **C03** now grades what matters — that nothing executed — because refusing a
   "no confirmation needed" request is as safe as proposing it for approval, and the router
   legitimately does either.

## What is still open

* **Table reading (L04).** Retrieval finds the table; the answer step picks the wrong row.
  Candidate fixes, in order of cost: render table rows as `label, column = value` at
  ingestion (Docling already does this, see [`chunking.md`](chunking.md)), or keep the
  row label in every child chunk.
* **Correcting a false premise (M01, M04).** Today the assistant abstains. Abstaining is
  safe, but a colleague would say "actually, it was 18 minutes". This is a product decision
  for the team lead, not a bug.
* **A 3B answering model** is the floor, not the target: these numbers are a lower bound,
  and the gateway can route the same cases to a larger model unchanged.
