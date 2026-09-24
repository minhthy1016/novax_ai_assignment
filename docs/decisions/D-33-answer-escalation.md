# D-33: Answer escalation — retry warning signs on a larger model, then ask the user

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

**Context.** The final D5 run scored 63/71 (66/71 read by hand) with a 3B answering model.
Of its eight failures, five were real defects of that model: E12 (correct but uncited), K08
(a false "not specified" caveat), M02 (did not correct a false premise), M01 and M04
(abstained on a false premise). The rest were grading artefacts: M03 and M05 were judge
errors, and E09 was a phrase-guard false positive. A larger model answers better but slower.
Every question on the larger model would pay that cost for the ~89% that the small model
already gets right.

**Decision.** Answer every question on the small, fast model. Escalate only the answers that
show a warning sign, and let the user help when no model can.

1. **Tier 1: the small model** (`llama3.2-3b`) answers from the retrieved sources, as before.
2. **Check the answer in code, with no model call.** The offline judge knows the reference
   answer; a live question does not. So escalation can only use signals visible in the answer
   itself:
   - **uncited:** the answer has no valid citation;
   - **not_covered:** the answer says the sources "do not mention / cover / specify" something.
3. **Tier 2: the larger model** (`OPSASSIST_ESCALATION_MODEL`, default `ollama/llama3.1-8b`)
   answers the same question from the same sources. Its answer is used only if it is cited
   and not an abstention; otherwise the first answer stands. **The egress rule applies
   unchanged:** confidential context stays on local models for the second call too. Both
   calls are counted in usage and in the attempt trail.
4. **Tier 3: ask the user.** An abstention, and a question with nothing relevant retrieved,
   end with what the caller can do. They can rephrase with more detail. If they hold
   `kb:write:<dept>`, they can upload the document for *their own* departments; otherwise
   they can ask their department's document owner. The hint never names documents or
   departments the caller cannot see.
   **A missing document becomes someone's task.** If the caller holds `ticket:create`, the
   response also carries a `suggested_action`: a knowledge-gap ticket for the document
   owner, containing only the caller's own question and department, with nothing
   retrieved. It is an offer, never created automatically. The console asks the caller to
   confirm, and the confirmation calls `POST /api/tickets`, which runs the same
   `create_support_ticket` tool: the same argument limits, permission check, idempotency
   (confirming twice gives one ticket) and audit record as a ticket raised in chat.
5. **Escalations are recorded:** the API response carries `escalation` (reason, first model,
   second model, outcome), the console shows it, and the eval report counts it. In
   production, the answers that escalate repeatedly become new eval cases. The judge stays
   offline.

**Abstentions are not escalated, because the data says so.** The first version also
escalated abstentions. On the 71-case suite the larger model rescued **0 of 11**. Nine of
those were correct refusals (another department's documents, no answer in the corpus, an
injection attempt), and each cost 4–8 s for the same result. Abstentions now go straight to
tier 3.

**Refusals in the model's own words are recognised.** The same runs showed the 3B model
sometimes declining in its own words ("I couldn't find any information on X in the provided
sources"). Unrecognised, that was shown as an uncited *answer* rather than a refusal, and it
triggered a pointless escalation (E03). An uncited answer of that shape is now treated as the
abstention it is.

## Measured (71 cases, clean stack each time, 3B answering, Qwen 7B judging)

| Run | Strict | Read by hand | Escalated | p50 / p95 |
|---|---|---|---|---|
| Frozen D5, no escalation | 63/71 | 66/71 | – | 1.09 / 4.51 s |
| Escalation v1 (abstentions escalated too) | 67/71 | 69/71 | 20 (28%) | 1.77 / 8.88 s |
| Escalation v2 (abstentions go to the user) | 65/71 | 68/71 | 10 (14%) | 1.08 / 6.95 s |
| **Escalation v3 (+ own-words refusals)** | **66/71** | **69/71** | **6 (8.5%)** | **0.92 / 7.09 s** |

The final report is [`escalation-run.md`](../../evaluation/reports/escalation-run.md). The
frozen D5 report is unchanged.

**The five real D5 failures, under v3:**

| Case | D5 failure | v3 |
|---|---|---|
| E12 | correct but uncited | ✅ passes. In v3 the small model cited on its own; in v1 escalation was what fixed it. |
| K08 | false "not specified" caveat | ✅ escalated (`not_covered`); the second answer is right: "API servers are patched one server at a time [1]". Still failed by the judge, which is a judge error. |
| M02 | did not correct the false premise | ✅ passes, by abstaining with the tier-3 hint (the case accepts an abstention). It still does not correct the premise. |
| M01, M04 | abstained on a false premise | ❌ still abstain, now with the tier-3 hint. A larger model does not fix this; it needs a premise-correction behaviour, which is a product decision. |

**Reading the numbers honestly:**
- **Real failures fall from five to two** (M01, M04). The remaining strict failures are judge
  errors: K08, M03, M05.
- **Escalation rate is 8.5%, and the larger model's answer was used every time.** Well
  under the ~15–20% at which a cascade stops paying for itself.
- **p95 latency roughly doubles for the escalated few.** On this laptop four models share
  16 GB and swap in and out of memory, so these latencies are inflated. With the models on
  their own GPUs (D-60), the second call costs one extra generation, not a model load.
- **Strict scores move 1–2 cases between runs** as the 3B model rewords answers. v1 vs v3
  (67 vs 66) is within that noise. v3 is kept because it escalates a third as often for the
  same number of real failures.

## Limits

- **Streaming answers are not escalated yet.** Their tokens have already reached the user,
  so correcting them needs a "revision" event the client understands. The console and the
  eval use the non-streaming endpoint.
- **The "not covered" signal is a phrase list.** It catches the partial-answer caveats seen
  in the suite, and legitimate partial answers too (K18). Those are escalated and re-answered
  at a small cost.
- **The knowledge-gap ticket has no routing to a named owner.** Tickets have no assignee
  today; the ticket is visible to the caller's department, which is where a document owner
  would pick it up.
- **Premise correction (M01, M04) is not addressed.** It is a behaviour change to decide
  with the team lead, not a model-size problem.

**Alternatives rejected:**
- **Always use the larger model:** it pays the latency and cost on the ~90% of questions
  the small model already answers correctly.
- **A runtime LLM verifier on every answer:** it adds a model call to every question, and
  without a reference answer it can only check grounding, which citation validation and
  re-pointing already cover deterministically.
