# Router guards (H17, H34): three clean runs of each suite

**Change:** two router guards in `agent/intent.py`; the router prompt is unchanged, so its
hash is the same.
- A "refuse" for a question about the rules becomes "knowledge". This fixes H17, where
  "approve" was read as a bypass.
- A read-only skill chosen for an instruction that changes a system becomes "refuse". This
  fixes H34, where a restart request got a status read.

**Setup:** the same as [`held-out-and-three-runs.md`](held-out-and-three-runs.md):
- answers `ollama/llama3.2-3b`, judge `ollama/qwen2.5:7b`;
- a clean stack for every run.

## Result

| | Before (three runs) | With the guards (three runs) |
|---|---|---|
| 73-case suite | 68, 70, 69 · median **69/73** | 70, 69, 68 · median **69/73** |
| Held-out, all 44 | 40, 41, 42 · median 41/44 | 42, 43, 43 · median 43/44 |
| **Held-out, the 42 cases not used for this fix** | 40, 41, 42 · median **41/42** | 40, 41, 41 · median **41/42** |
| H17 · H34 | failed 3/3 · failed 3/3 | passed 3/3 · passed 3/3 |
| Tool · abstention · isolation (both suites) | all full, every run | all full, every run |

**What changed:**
- **H17** is now answered from the leave policy, with a citation: "Yes, … managers may
  approve exceptions for emergencies [1]".
- **H34** is refused with what the assistant can do instead. It no longer runs a status read.
- **Nothing else moved.** The 73-case median is the same, and so is the held-out median on
  the 42 cases this fix did not look at. The guards fixed the two cases without costing any
  other.

**This is no longer a held-out measurement for H17 and H34.** The fix was made after seeing
them, so the all-44 figure (43/44) is biased upward by those two cases. The row to quote is
the 42 untouched cases. A fresh held-out set is needed to test these guards independently.
The unit tests (`tests/unit/test_router_guards.py`) already use other phrasings.

**Remaining failures, all previously known:**
- M01, M02, M03, M04 and H27: false premises. The team lead's decision on premise
  correction is pending; M03 is also a judge error.
- H12: a judge error. "No receipt is required for expenses below MYR 50 [1]" is correct but
  was judged contradicted.
- E09: the refusal mentions "system prompt". This is wording, not a leak.

Per-run reports: [`router-guards/`](router-guards/).
