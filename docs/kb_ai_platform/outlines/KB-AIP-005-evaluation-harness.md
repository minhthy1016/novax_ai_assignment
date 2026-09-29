```yaml
document_id: KB-AIP-005
title: Evaluation Harness Design
department: ai_platform
classification: internal
status: outline
derived_from:
  - docs/decisions/D-34-prompts-hold-rules-cases-never-enter-prompts.md
  - docs/decisions/D-50-evaluation-design.md
  - evaluation/run_eval.py
  - evaluation/judge.py
```

# Evaluation Harness Design

**Scope:** how the assistant is measured: the method only. This page holds no scores, no
cases and no results; those live in the repository's evaluation reports.

## 1. What is measured
- **The brief's axes:**
  - answer correctness;
  - retrieval relevance;
  - citation correctness;
  - hallucination and abstention;
  - tool accuracy;
  - performance;
  - cost.
- **Each case is one named employee asking one question,** with the expected sources,
  forbidden sources, expected tool and arguments, expected outcome, reference facts and
  phrases that must not appear.

## 2. Four sets, four purposes
- **The tuned suite:** the cases the system was developed against.
- **The held-out suite:** written after tuning, frozen in a commit before it first ran,
  and used to check that fixes generalise. A case used to fix something no longer counts
  as held out.
- **Hand labels for the judge:** fixed answers labelled by a person, used to measure the
  judge itself.
- **The retrieval gold set:** questions paired with the exact source text that answers
  them.
- **None of the sets is used for training.**

## 3. Two kinds of grader
- **Rules are compared exactly, in code:** tool choice and arguments, authorization,
  pending versus executed, isolation, abstention, citation validity, forbidden phrases.
- **Only prose is judged by a model.** Three separate questions:
  - is this reference fact conveyed;
  - does this citation support this sentence;
  - did the answer assert anything outside its sources.
- **The judge is independent of what it grades:**
  - it sits outside the system under test and calls its model directly;
  - it comes from a different model family from the answering model;
  - it runs locally, so judging a confidential answer never leaves the machine.
- **The judge must quote the answer** to call a fact contradicted. Code then discards the
  verdicts it can check itself: a contradiction whose own quote states the fact, and an
  "unsupported" claim whose words and figures are in a passage.

## 4. Keeping the measurement honest
- **Prompts hold rules, not cases.** A test fails if any prompt shares wording with an
  evaluation question or answer, or names a sample record.
- **Every report records the prompt hashes and the suite file with its hash.**
- **Single runs are noisy, so a headline is the median of three clean runs,** each on a
  freshly reset stack, with the range beside it.
- **Every rate has a 95% Wilson interval,** and every failing case is printed with its
  answer, so a reader can overrule the judge.
- **A control baseline** (the same model, no retrieval, no policy) shows what the pipeline
  adds.

## 5. Running it
- `make eval`: the tuned suite with the judge.
- `make eval-fast`: the code-graded axes only.
- `run_eval --cases <file> --report <name>`: another suite, into its own report.

## Excluded from this page
- Every score, median, range, interval value and judge-agreement figure.
- Every case: its id, question, reference facts and failure analysis.
