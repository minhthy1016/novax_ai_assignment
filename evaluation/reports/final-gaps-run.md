# Regression check after the final fixes

**Changes checked:**
- Egress is decided on the whole conversation (migration 0009).
- `/api/chat/stream` runs the agent first.
- Tool calls are exported as metrics.
- Documentation of chunk overlap.

None of them changes what the evaluation exercises. Each case is a fresh conversation, and
the prompts and router are unchanged. So one clean run of each suite checks for a regression;
the headline stays the median of three
([`router-guards-run.md`](router-guards-run.md)).

| | Three-run range before | This run |
|---|---|---|
| 73-case suite | 68–70/73 (median 69) | **70/73** |
| Held-out, all 44 · 42 untouched | 42–43/44 · 40–41/42 | **43/44 · 41/42** |
| Tool · abstention · isolation | 34/34 · 12/12 · 10/10 | 34/34 · 12/12 · 10/10 |
| Held-out tool · abstention · isolation | 15/15 · 9/9 · 6/6 | 15/15 · 9/9 · 6/6 |
| Citations valid · phrase guards (73 cases) | 100% · 28–29/29 | 39/39 · 29/29 |
| p50 / p95 (73 cases) | 1.2 s / 7.3–7.8 s | 1.2 s / 6.9 s |

**No regression.** Both scores are within the earlier range, and every axis graded by code is
unchanged.

**Failures, all previously known:**
- M01, M04 and H27: false premises not corrected.
- M03: a judge error.

The new behaviour is covered by tests rather than by the suite:
- `test_confidential_context_keeps_the_whole_conversation_on_the_box` (security,
  integration, with a fresh conversation as the control);
- `test_streaming_goes_through_the_agent`;
- `test_tool_calls_are_exported_as_metrics` and
  `test_tool_calls_are_counted_with_bounded_labels`.

Per-suite reports: [`final-gaps/`](final-gaps/).
