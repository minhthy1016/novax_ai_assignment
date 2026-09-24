# Judge drift: the same answers, two judge versions

Answers: `eval-20260923-1625.json` (the frozen D5 run) · judge model `ollama/qwen2.5:7b` · judge-v1 = prompt at `2c4cd13` (`d8d53fb9ab0a`) · judge-v2 = current prompt (`6d9baee4874b`) · hand labels: `evaluation/judge_labels.jsonl`.

| Comparison | Agreement |
|---|---|
| v1 recorded vs v1 prompt re-run under current checks | 37/39 |
| v1 prompt vs v2 prompt, both under current checks (rubric change) | 38/39 |
| v1 recorded vs hand label | 35/39 |
| v1 prompt, current checks vs hand label | 35/39 |
| **v2 prompt, current checks vs hand label** | **36/39** |

Verdict flips from v1 (re-run) to v2: contradicted → missing: 1.

## Every fact where a judge disagrees with the hand label

| Case | Category | Label | v1 recorded | v1 prompt now | v2 | v2 quote | Note |
|---|---|---|---|---|---|---|---|
| K08 | negation | supported | contradicted | unusable | unusable | API servers are patched one server at a time | fact conveyed, next to a false 'not specified' caveat |
| M02 | negation | missing | contradicted | contradicted | missing |  | 'does not mention Fridays' - the window itself is never stated |
| M03 | negation | supported | contradicted | contradicted | contradicted | No, according to the Annual Leave Policy [1], employees rece | 'No, ... 14 days' - a correct answer that opens with a negation |
| M05 | plain | supported | contradicted | unusable | unusable | Feature flags may be switched off without approval during a  |  |

## By category (right / total)

| Category | Facts | v1 re-run | v2 |
|---|---:|---:|---:|
| entailed | 4 | 4 | 4 |
| negation | 5 | 2 | 3 |
| partial | 4 | 4 | 4 |
| plain | 24 | 23 | 23 |
| refusal | 1 | 1 | 1 |
| scope | 1 | 1 | 1 |
