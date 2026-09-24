"""Judge drift: the same answers graded by two versions of the fact judge.

A changed rubric makes old and new scores two different measurements. This script keeps the
answers fixed - the ones a run already recorded - and re-grades every reference fact with
judge-v1 (the prompt as of a given commit) and judge-v2 (the current prompt). Three
comparisons come out of it:

* v1 as recorded vs the v1 prompt re-run: run-to-run noise, plus any grading checks added in
  code since (they apply to both prompts on a re-run);
* v1 re-run vs v2: how far the new rubric moves the decision boundary;
* each version vs the hand labels in ``evaluation/judge_labels.jsonl``: which one is right.

Passages are fetched again from ``/api/search`` for the case's own actor, exactly as the
harness does, so the stack must hold the same corpus the run used (a clean
``make reset && make up && make ingest``).

Usage:
    python -m evaluation.judge_drift --run evaluation/runs/eval-20260923-1625.json \\
        --v1-commit 2c4cd13
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

import evaluation.judge as judge_module
from evaluation.judge import Judge

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "evaluation/reports/judge-drift.md"


def v1_prompt(commit: str) -> str:
    # A developer tool reading the repository's own history; the commit comes from its CLI.
    source = subprocess.run(  # noqa: S603
        ["git", "show", f"{commit}:evaluation/judge.py"],  # noqa: S607
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    match = re.search(r'JUDGE_PROMPT = """(.*?)"""', source, re.S)
    if not match:
        raise SystemExit(f"no JUDGE_PROMPT in evaluation/judge.py at {commit}")
    return match.group(1)


def passages(api: httpx.Client, actor: str, question: str, tokens: dict[str, str]) -> list[str]:
    if actor not in tokens:
        tokens[actor] = api.post("/api/auth/dev-token", json={"user_id": actor}).json()[
            "access_token"
        ]
    found = api.post(
        "/api/search",
        json={"query": question, "top_k": 4},
        headers={"Authorization": f"Bearer {tokens[actor]}"},
    )
    return [h["context"] for h in found.json().get("hits", [])] if found.status_code == 200 else []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--v1-commit", required=True, help="commit whose judge prompt is v1")
    parser.add_argument("--judge", default="ollama/qwen2.5:7b")
    parser.add_argument("--api", default="http://localhost:8000")
    args = parser.parse_args()

    run = json.loads(args.run.read_text())["results"]
    cases = {
        c["case_id"]: c
        for c in map(json.loads, (ROOT / "evaluation/cases.jsonl").read_text().splitlines())
    }
    labels = [
        json.loads(line)
        for line in (ROOT / "evaluation/judge_labels.jsonl").read_text().splitlines()
    ]
    prompts = {"v1": v1_prompt(args.v1_commit), "v2": judge_module.JUDGE_PROMPT}
    judge = Judge(args.judge)
    tokens: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    index = 0
    with httpx.Client(base_url=args.api, timeout=120) as api:
        for result in run:
            case = cases[result["case_id"]]
            if not result["facts"]:
                continue
            sources = passages(api, case["actor_id"], case["prompt"], tokens)
            for recorded in result["facts"]:
                label = labels[index]
                index += 1
                assert label["fact"] == recorded["fact"], (label, recorded)
                row: dict[str, Any] = {
                    **label,
                    "answer": result["answer"],
                    "v1_recorded": recorded["verdict"],
                }
                for version, prompt in prompts.items():
                    judge_module.JUDGE_PROMPT = prompt  # judge_fact reads the module constant
                    verdict = judge.judge_fact(
                        case["prompt"], recorded["fact"], result["answer"], sources
                    )
                    row[f"{version}_rerun"] = verdict.verdict or "unusable"
                    row[f"{version}_quote"] = verdict.quote
                rows.append(row)
                print(
                    f"{row['case_id']:4} label={row['label']:9} v1={row['v1_recorded']}"
                    f"/{row['v1_rerun']} v2={row['v2_rerun']}"
                )
    judge_module.JUDGE_PROMPT = prompts["v2"]
    REPORT.write_text(report(rows, args, prompts))
    (ROOT / "evaluation/runs/judge-drift.json").write_text(json.dumps(rows, indent=1))
    print(f"\nWritten: {REPORT.relative_to(ROOT)}")


def report(rows: list[dict[str, Any]], args: argparse.Namespace, prompts: dict[str, str]) -> str:
    n = len(rows)

    def agree(a: str, b: str) -> int:
        return sum(r[a] == r[b] for r in rows)

    def correct(col: str) -> int:
        return sum(r[col] == r["label"] for r in rows)

    flips = Counter(
        f"{r['v1_rerun']} → {r['v2_rerun']}" for r in rows if r["v1_rerun"] != r["v2_rerun"]
    )
    short = {k: hashlib.sha256(v.encode()).hexdigest()[:12] for k, v in prompts.items()}
    lines = [
        "# Judge drift: the same answers, two judge versions",
        "",
        f"Answers: `{args.run.name}` (the frozen D5 run) · judge model `{args.judge}` · "
        f"judge-v1 = prompt at `{args.v1_commit}` (`{short['v1']}`) · judge-v2 = current "
        f"prompt (`{short['v2']}`) · hand labels: `evaluation/judge_labels.jsonl`.",
        "",
        "| Comparison | Agreement |",
        "|---|---|",
        "| v1 recorded vs v1 prompt re-run under current checks | "
        f"{agree('v1_recorded', 'v1_rerun')}/{n} |",
        "| v1 prompt vs v2 prompt, both under current checks (rubric change) | "
        f"{agree('v1_rerun', 'v2_rerun')}/{n} |",
        f"| v1 recorded vs hand label | {correct('v1_recorded')}/{n} |",
        f"| v1 prompt, current checks vs hand label | {correct('v1_rerun')}/{n} |",
        f"| **v2 prompt, current checks vs hand label** | **{correct('v2_rerun')}/{n}** |",
        "",
        "Verdict flips from v1 (re-run) to v2: "
        + (", ".join(f"{k}: {v}" for k, v in flips.most_common()) or "none")
        + ".",
        "",
        "## Every fact where a judge disagrees with the hand label",
        "",
        "| Case | Category | Label | v1 recorded | v1 prompt now | v2 | v2 quote | Note |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if len({r["label"], r["v1_recorded"], r["v1_rerun"], r["v2_rerun"]}) > 1:
            quote = (r["v2_quote"] or "")[:60].replace("|", "/")
            lines.append(
                f"| {r['case_id']} | {r['category']} | {r['label']} | {r['v1_recorded']} | "
                f"{r['v1_rerun']} | {r['v2_rerun']} | {quote} | {r['note']} |"
            )
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r)
    lines += [
        "",
        "## By category (right / total)",
        "",
        "| Category | Facts | v1 re-run | v2 |",
        "|---|---:|---:|---:|",
    ]
    for cat, group in sorted(by_cat.items()):
        lines.append(
            f"| {cat} | {len(group)} | {sum(r['v1_rerun'] == r['label'] for r in group)} | "
            f"{sum(r['v2_rerun'] == r['label'] for r in group)} |"
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
