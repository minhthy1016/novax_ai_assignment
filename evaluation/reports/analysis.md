# What the evaluation run actually showed

Companion to the generated [`evaluation.md`](evaluation.md). The report is produced by the
harness; this file is the part a machine cannot write: every failure read by hand, and what
it means. Run of 2026-09-23, 71 cases, `ollama/llama3.2-3b` answering, `ollama/qwen2.5:7b`
judging, ~8 minutes wall clock with the per-citation checks and the control included.
Report: [`evaluation.md`](evaluation.md), raw results `evaluation/runs/eval-20260923-1524.json`.
The scores below are the harness's own output; nothing was adjusted by hand. The hand-read
score is a separate column, and every case in it has a stated reason.

## Headline, in one line

Nothing leaked and nothing executed that should not have: **isolation 10/10, tool accuracy
30/30, abstention 12/12, citations 38/38 valid** — and the
answering model got a handful of prose questions wrong or gave up on them, which is the
honest state of a 3-billion-parameter model reading tables.

## Every failure, read by hand

| Case | What happened | Verdict |
|---|---|---|
| **K08** | "How many API servers may we patch at the same time?" — "API servers are patched one server at a time [1]. This source does not cover how many API servers may be patched at the same time." | **Real: a false caveat.** The first sentence is the answer; the second contradicts it. It comes from the partial-answer rule (below). The judge also failed it, but for a wrong reason: it said the answer mentions web servers, which it does not. |
| **M02** | "Why do we deploy to production on Fridays?" — "The source does not mention deploying on Fridays. It actually specifies … Tuesday or Thursday, 21:00-23:00 MYT." | **Real: correct, but uncited.** The premise is now corrected, which is the right content, but no `[n]` marker was written, so the answer reaches the user without a source. The harness counts an uncited answer as a miss, which is what caught this. |
| **M01** | "Since the August incident lasted three hours, what compensation did we pay?" — abstained. | **Real but defensible.** Nothing in the corpus supports "three hours" or any compensation, so it declines rather than inventing. Correcting the premise ("it was 18 minutes") would be better. |
| **M04** | "Which incident commander was assigned during the August incident?" — abstained, and retrieval returned the runbook rather than the incident report. | **Real, small.** The right answer is "the report names none". Abstention is safe; the retrieval choice is the actual defect. |
| **M03** | "Employees get 21 days, right?" — "No, according to the Annual Leave Policy [1], employees receive 14 days of annual leave, not 21 days." The judge marked the fact *contradicted*. | **Judge error.** The system was right. |
| **M05** | The answer correctly reported that feature flags need no approval during SEV1/SEV2; the judge marked it contradicted. | **Judge error.** The system was right. |
| **E10** | "Did duplicate charges occur?" — "No, it was not confirmed that duplicate charges occurred [1]." | **Guard false positive.** The `must_not_contain` phrase "duplicate charges occurred" appears inside the correct, negated answer. |

**So: four real defects, two judge errors, one guard false positive.** Scored strictly the
system is **64/71**; read by hand it is **67/71** (the three cases above marked judge error or
guard false positive are the only difference). Case-level totals move by one or two between
runs as the 3B model rewords an answer; the deterministic axes do not move. Across the four
runs made while fixing the defects below, the strict score was 64–66/71.

### Fixed: L03, a correct fact credited to the wrong source

"What triggers scaling for payment-worker?" was answered correctly — queue depth above
5,000 — but credited to the incident notes ([2], [3]) instead of the rate card ([1]), which
retrieval ranked first and which is the only source containing "5,000". The model matched on
"worker scaling". It did this in 5 of 6 repeats.

The fix is deterministic and needs no second model call (`rag.repoint_citations`, D-20). After
the model answers, each sentence's figures are compared with the sections its markers cite.
If a figure is in none of them but is in another retrieved section, the citation moves there.
L03 now cites the rate card 6/6 live, and in this run: "According to source [1], the
payment-worker is scaled when the queue depth is above 5,000." Control questions are
untouched. The report counts every correction ("Citations re-pointed by the backend": one
answer, two sources in this run), so it is visible, not silent.

### Fixed: L04, reading a value off the wrong table row

In the previous run L04 asked how fast Tier 2 must acknowledge a **SEV1** and was told **30
minutes**: the table's SEV1 row says 10, 30 is the row above. Retrieval had found the table;
the text layer had flattened it to `Tier 2 - platform 24/7 30 minutes 9,600` with the column
names lines away, and the 3B model matched the first row label it saw.

The PDF parser now rebuilds tables from the geometry the PDF still has (each cell is a text
run with an x position) and writes every row with its own labels —
`Tier 2 - platform (SEV1): Hours = 24/7; Acknowledge within = 10 minutes; ...` — one block per
row, so no chunk can split a value from its row (D-20). Result: L04 answers **10 minutes**,
3/3 on repeat; the neighbouring question ("a normal page") answers 30 minutes 3/3, so the
model is reading rows, not favouring a new number. Retrieval did not suffer (hybrid Hit@1
0.878 → 0.927, intervals overlapping — see [`chunking.md`](chunking.md)). Ranking the rate card
first is also what exposed L03 (fixed above).

## Citation correctness, measured the way the brief words it

The brief asks that a citation "supports the exact claim and resolves to a source", which is
stronger than what we measured first. There are now two checks:

* **Resolves** (deterministic): every cited source was actually retrieved for this caller —
  38/38.
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

In this run it is **32/38 = 0.842 (0.70–0.93)**. The misses: **L03** ×1 — not the re-pointed
fact sentence, which is now supported, but an added "Source [4] does not cover this…"; **M03**
(the judged sentence is correct; the judge disagreed); **I02** (a refusal that names the
source it refused); **K01**, **K05** and **L02** (the cited section does contain the claim;
the judge disagreed).

## What this says about the judge

The judge earned its place and also showed its limits.

* It caught **L04** (in the previous run), which no string check would have: the answer was
  fluent, cited a real source, and was wrong by one table row. The per-citation check also
  caught **L03**, where the fact is right and only the attribution is wrong.
* It twice called a *correct* answer "contradicted" because the answer opened with a
  correction ("No, …"). The quote requirement — a contradiction must be quotable from the
  answer — cut this class of error down from three cases to two, but did not remove it.
* Requiring a quote for `supported` as well was tried and reverted: it discarded 12 verdicts
  for answers that were correct but paraphrased, which is the very thing a judge is for.

The honest conclusion is that **a 7B judge is good enough to find defects and not good enough
to be the final word**, which is why every failing case in the generated report prints the
answer verbatim. A reviewer can overrule it in seconds; nobody has to trust it.

## What the control shows

The same model, same questions, no retrieval and no policy: it stated **16%** of the
reference facts (vs 87% through the pipeline), produced no citations at all, and answered
**5 of 5** questions that the caller had no right to have answered — including the HR
compensation notes, which it cheerfully described from imagination.

The point is not that the control is bad at facts. It is that nothing it says can be
checked, and it has no notion of who is asking.

## Two defects found by hand in the console, and what fixing them cost

Both came from trying the HR role in the console, not from the suite — which is itself a
finding: the suite had a blind spot for the first one.

**1. A correct answer with no citation.** "How far in advance should a three-day leave request
be submitted?" came back right but uncited: the model wrote "according to source 1", and only
`[1]` was recognised. Fix: prose references ("source 1", "source #2 [2]") are rewritten to
`[n]` when *n* is a source that was provided (`rag.normalize_markers`), the prompt now asks for
`[1]` explicitly, and the console shows an **uncited** badge on any knowledge answer that
still has no citation. **The harness had the same blind spot:** "expected source cited" was
only checked when an answer cited *something*, so an uncited answer passed. It is now a
failure.

**2. All-or-nothing on two-part questions.** "How many annual leave and sick leave days?"
abstained, because the corpus has no sick-leave policy and the prompt said to abstain when
"the sources do not contain the answer". Four wordings were tried against live retrieval
before running the suite:

| Prompt rule | Single-part questions | Two-part questions |
|---|---|---|
| original: abstain if the answer is not in the sources | clean | abstains on the whole question |
| "answer what you can, then name what is not covered" (with a *sick leave* example) | adds a false caveat 3/3 | drops the half it could answer |
| "answer what you can; no need to comment on the rest" | clean | abstains 2/3, or answers uncited |
| **"if the question asks about two or more separate things and the sources cover only some, answer those with citations, then say in one sentence which are not covered"** | adds a caveat ~1/3 | **correct 5/6** |

The last one is in production, and a case for it is in the suite (**K18**, now: "Employees
receive 14 days of annual leave [1]. The source does not mention sick leave."). The cost is
real and visible in this run: the 3B model sometimes appends "source X does not cover …" to
a question it answered in full (K08, L04), and in **M02** it reported what is missing instead
of correcting the premise. The prompt with the "no need to comment" rule scored one case
higher strictly (66/71) but its answers were worse by hand — K18 uncited with sick leave
silently dropped, M03 a bare "No [1]." — so the strict score was not the deciding number.

**3. The harness miscounted guards.** A case with two `must_not_contain` guards was counted
once per guard but scored pass/fail as a whole, so one failed phrase counted as two. Guards
are now counted one by one.

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

These are known limitations. The methodology is frozen; they are recorded, not being tuned.

* **Attribution without figures.** `repoint_citations` corrects only claims that state a
  figure. A claim with no numbers ("rollbacks need no approval") can still be credited to the
  wrong source; the per-citation judge measures it, nothing corrects it at runtime.
* **Unrequested "does not cover" sentences (K08, L03).** The partial-answer rule is applied
  too eagerly by a 3B model. Usually it is noise (L03); in **K08** it is false — it says the
  source does not cover what the previous sentence just answered from it. Candidate fixes: a
  larger answering model (the gateway routes the same cases unchanged), or a structured
  answer (`answered` / `not_covered` fields) that the backend renders.
* **Uncited answers (M02).** An answer can still reach the user without a citation when the
  model writes none at all. The console marks it **uncited** and the harness counts it as a
  miss; nothing blocks it.
* **Tables the parser does not handle.** Rows are self-labelled only for born-digital tables
  of three or more columns; merged or multi-line cells, a header continued across a page
  break and scanned pages still come through as text (D-20).
* **Correcting a false premise (M01, M04).** Today the assistant abstains. A colleague would
  say "actually, it was 18 minutes". This is a product decision for the team lead, not a bug.
* **A 3B answering model** is the floor, not the target: these numbers are a lower bound.
