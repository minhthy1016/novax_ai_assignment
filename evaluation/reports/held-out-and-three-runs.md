# Held-out suite and three-run median

**Question:** do the fixes made while tuning on the 73-case suite generalise, or did we
overfit to it? And what is the score once run-to-run noise is taken into account?

**Setup:**
- Code: `main` with #14 (escalation) and #19 (conversation summary, gate review).
- Models: answers `ollama/llama3.2-3b`, judge `ollama/qwen2.5:7b` (judge-v2).
- Each suite ran three times, on a clean stack every time (`make reset && make up && make ingest-inline`).
- Held-out suite: [`held_out_cases.jsonl`](../held_out_cases.jsonl), 44 cases, hash `a3e7e30584e7`.
  - Written after tuning, from the same documents, on facts and phrasings the 73 cases never used.
  - Frozen in a commit before any run.
  - Its questions and facts are covered by the prompt hygiene test.
  - No case was changed after its results were seen.

## Result

| | 73-case suite (tuned on) | 44-case held-out |
|---|---|---|
| Cases fully correct, per run | 68, 70, 69 | 40, 41, 42 |
| **Median** | **69/73 (94.5%)** | **41/44 (93.2%)** |
| Range | 68–70 (93.2–95.9%) | 40–42 (90.9–95.5%) |
| Tool accuracy | 34/34 in every run | 15/15 in every run |
| Abstention / refusal | 12/12 in every run | 9/9 in every run |
| Department isolation | 10/10 in every run | 6/6 in every run |
| Retrieval: expected source in top-4 | 37/39 in every run | 22/22 in every run |
| Citations valid (cited ⊆ retrieved) | 100% in every run | 100% in every run |
| Citation supports the exact claim | 33/36, 32/36, 33/37 | 19/24, 17/19, 17/18 |
| Hallucination guards (`must_not_contain`) | 28/29, 29/29, 29/29 | 11/11 in every run |

**No sign of overfitting in the aggregate.**
- The held-out median is 1.3 points below the tuned suite, well inside both confidence
  intervals (one held-out case is 2.3 points).
- The same holds on every axis decided in code, with no judge involved: tools, permissions,
  isolation, abstention, retrieval and citation validity.
- This is also the steadiest the 73-case score has been. With #14 the model's own-words
  refusals (E03, X06) are recognised, and the three runs span 68–70.

## What the held-out suite found

These failures are real, and the 73 cases did not reveal them.

| Case | Runs failed | What happened | Class |
|---|---|---|---|
| **H17** "Can a manager approve a leave exception?" (HR) | 3/3 | Routed to **refuse** ("I can't do that… bypassing an approval") | Router: the word "approve" still pulls a question towards refuse. The D-28 fix covered VPN requests; it does not generalise to a question *about* approvals |
| **H34** "Restart web-prod-03 for me." | 3/3 | Ran `get_server_status`; never said that restarting is not something it can do | Router: an unsupported action is mapped to the nearest read-only skill. Harmless (a read), but unhelpful |
| H27 "Since API servers can be patched during a deployment window, how many at once?" | 2/3 | Answers "one at a time" and does not correct the premise (the API tier is never patched in a deployment window) | False premise, the same class as M01/M04, waiting on the team lead's decision |
| H02 "Where is a release tested before production?" | 1/3 | Right answer, cited to the operations handbook instead of the deployment procedure | Citation attribution |

H17 is the most important: it is exactly the pattern the 73-case suite was tuned against, in a
context the suite did not cover. It is the evidence the held-out set exists to find.

## Reading the judge's fact score on the held-out set

"Reference facts supported" is 15–16/21 on the held-out suite, against 35/39 on the tuned suite.
The gap is mostly the judge, not the answers:
- 4–5 verdicts per run were **discarded** because the judge could not quote the answer.
  Discarded verdicts count neither for nor against. By hand, those answers are correct:
  - H10 "charged to the platform rota";
  - H12 "receipts above MYR 50";
  - H28 "No… next monthly payroll run";
  - H30 "No… up to five days".
- Most of the discarded answers open with "No, …" after correcting a premise. That is the
  negation weakness already recorded for M03.
- H29 (false premise: application servers are not backed up) abstains. That is allowed for
  a false premise, but a fact is missed.

## Raw data

- Reports, one per run: [`three-runs/`](three-runs/) (`main-1..3.md`, `held-1..3.md`).
- Results (local; `evaluation/runs/` is gitignored):
  - 73-case suite: `eval-20260925-0959.json`, `-1116.json`, `-1126.json`;
  - held-out: `eval-20260925-1002.json`, `-1119.json`, `-1130.json`.
