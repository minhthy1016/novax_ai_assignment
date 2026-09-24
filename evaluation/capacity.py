"""Capacity model behind the scale proposal (docs/decisions/D-60-scale-proposal-aws.md).

Every figure in the proposal's sizing tables is computed here from named inputs, and every
input says whether it was measured in this repository or assumed. Changing an assumption
and re-running regenerates the tables; `tests/unit/test_capacity.py` fails if D-60 quotes a
number this model no longer produces.

The corpus density is measured live from ``sample_data/knowledge`` with the production
parser and chunker. The other measurements are constants with their provenance, because the
runs they come from (``evaluation/runs/``) are not committed.

Usage:
    python -m evaluation.capacity            # print the tables
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from opsassist.knowledge.chunking import chunk_document
from opsassist.knowledge.parsing import SUPPORTED_SUFFIXES, parse_file
from opsassist.providers.base import estimate_tokens

KNOWLEDGE = Path(__file__).resolve().parents[1] / "sample_data" / "knowledge"
M, A = "measured", "assumed"


@dataclass(frozen=True)
class Inputs:
    # ---- demand (the brief)
    employees: int = 5_000
    documents: int = 1_000_000
    concurrent_requests: int = 100
    departments: int = 10  # A: the sample has 5; a 5,000-person company has more
    # ---- corpus
    avg_document_tokens: int = 2_500  # A: ~5 pages; the samples average ~290 tokens
    # ---- answers
    prompt_tokens: int = 1_100  # M: p95 of knowledge answers in the final D5 run (mean 695)
    output_tokens: int = 300  # A: a 20B-class model; the 3B model measured 29 on average
    stream_tokens_per_s: int = 40  # A: per-stream decode rate users perceive as fluent
    answers_per_employee_day: int = 5  # A
    # ---- inference (planning figures for vLLM on one L4; the load test replaces them)
    gpu_decode_tokens_per_s: int = 1_000  # A: aggregate, continuous batching
    gpu_prefill_tokens_per_s: int = 8_000  # A
    gpu_target_utilisation: float = 0.6  # A: headroom for bursts before shedding
    gpus_per_node: int = 4  # g6.12xlarge
    # ---- embeddings
    embed_laptop_chunks_per_s: int = 164  # M: nomic-embed-text, Ollama, batch 128, Apple M5
    embed_gpu_chunks_per_s: int = 2_000  # A: same model on one L4
    daily_changed_fraction: float = 0.01  # A: 1% of documents change per day
    # ---- storage (pgvector halfvec(768); each chunk row also stores its section's text)
    vector_bytes: int = 768 * 2
    chunk_text_bytes: int = 250  # M: chunks target ~64 tokens
    section_text_bytes: int = 1_000  # M: sections cap at ~256 tokens
    row_overhead_bytes: int = 250  # A: metadata columns, tuple header, TOAST pointers
    hnsw_bytes_per_row: int = 1_800  # A: pgvector HNSW (m=16) stores the vector plus links


def chunks_per_1k_tokens() -> float:
    """Measured: search chunks (the small, embedded passages) the production chunker makes
    per 1,000 tokens of text."""
    tokens = chunks = 0
    for path in sorted(KNOWLEDGE.glob("KB-*")):
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        doc = parse_file(path)
        tokens += sum(estimate_tokens(b.text) for b in doc.blocks)
        chunks += len(chunk_document(doc))
    return chunks / tokens * 1_000


@dataclass(frozen=True)
class Sizing:
    density: float
    chunks: float
    heap_gb: float
    hnsw_gb: float
    hnsw_gb_per_department: float
    answer_seconds: float
    answers_per_s_at_ceiling: float
    answers_per_day: float
    answers_per_s_average: float
    decode_tokens_per_s: float
    prefill_tokens_per_s: float
    gpus: int
    gpu_nodes: int
    tokens_per_day: float
    initial_index_hours_gpu: float
    initial_index_days_laptop: float
    daily_reindex_minutes_gpu: float


def size(i: Inputs | None = None) -> Sizing:
    i = i or Inputs()
    density = chunks_per_1k_tokens()
    chunks = i.documents * i.avg_document_tokens / 1_000 * density
    row = i.vector_bytes + i.chunk_text_bytes + i.section_text_bytes + i.row_overhead_bytes
    heap_gb = chunks * row / 1e9
    hnsw_gb = chunks * i.hnsw_bytes_per_row / 1e9

    # Little's law at the brief's ceiling: 100 requests in flight, each streaming its answer.
    answer_seconds = i.output_tokens / i.stream_tokens_per_s + 1.0  # + retrieval and prefill
    at_ceiling = i.concurrent_requests / answer_seconds
    per_day = i.employees * i.answers_per_employee_day
    decode = i.concurrent_requests * i.stream_tokens_per_s
    prefill = at_ceiling * i.prompt_tokens
    gpu_time = decode / i.gpu_decode_tokens_per_s + prefill / i.gpu_prefill_tokens_per_s
    gpus = math.ceil(gpu_time / i.gpu_target_utilisation)
    nodes = math.ceil(gpus / i.gpus_per_node) + 1  # N+1: lose a node or an AZ and keep serving

    return Sizing(
        density=density,
        chunks=chunks,
        heap_gb=heap_gb,
        hnsw_gb=hnsw_gb,
        hnsw_gb_per_department=hnsw_gb / i.departments,
        answer_seconds=answer_seconds,
        answers_per_s_at_ceiling=at_ceiling,
        answers_per_day=per_day,
        answers_per_s_average=per_day / (8 * 3600),
        decode_tokens_per_s=decode,
        prefill_tokens_per_s=prefill,
        gpus=gpus,
        gpu_nodes=nodes,
        tokens_per_day=per_day * (i.prompt_tokens + i.output_tokens),
        initial_index_hours_gpu=chunks / i.embed_gpu_chunks_per_s / 3600,
        initial_index_days_laptop=chunks / i.embed_laptop_chunks_per_s / 86_400,
        daily_reindex_minutes_gpu=chunks * i.daily_changed_fraction / i.embed_gpu_chunks_per_s / 60,
    )


def tables(i: Inputs | None = None) -> str:
    i = i or Inputs()
    s = size(i)
    rows_in = [
        (
            "Search chunks per 1,000 tokens",
            f"{s.density:.1f}",
            M,
            "production chunker on the sample corpus",
        ),
        (
            "Average document length",
            f"{i.avg_document_tokens:,} tokens",
            A,
            "~5 pages; samples average ~290",
        ),
        (
            "Prompt tokens per answer",
            f"{i.prompt_tokens:,}",
            M,
            "p95 of the final D5 run (mean 695)",
        ),
        (
            "Output tokens per answer",
            f"{i.output_tokens}",
            A,
            "20B-class model; the 3B model averaged 29",
        ),
        ("Per-stream decode rate", f"{i.stream_tokens_per_s} tok/s", A, "fluent streaming"),
        (
            "L4 decode / prefill, vLLM",
            f"{i.gpu_decode_tokens_per_s:,} / {i.gpu_prefill_tokens_per_s:,} tok/s",
            A,
            "planning figure; load test replaces it",
        ),
        (
            "Embedding throughput",
            f"{i.embed_laptop_chunks_per_s}/s laptop · {i.embed_gpu_chunks_per_s:,}/s L4",
            "measured · assumed",
            "nomic-embed-text, batch 128",
        ),
        ("Answers per employee per day", f"{i.answers_per_employee_day}", A, ""),
        ("Departments", f"{i.departments}", A, "partitions of the vector tier"),
    ]
    rows_out = [
        (f"Search chunks at {i.documents / 1e6:.0f}M documents", f"~{s.chunks / 1e6:.0f} M"),
        ("Chunk table (heap)", f"~{s.heap_gb:.0f} GB"),
        ("HNSW index, all departments", f"~{s.hnsw_gb:.0f} GB"),
        ("HNSW index per department partition", f"~{s.hnsw_gb_per_department:.1f} GB"),
        ("Answer duration at the ceiling", f"~{s.answer_seconds:.1f} s"),
        (
            f"Answers/s with {i.concurrent_requests} in flight",
            f"~{s.answers_per_s_at_ceiling:.1f}",
        ),
        (
            "Answers/day expected · average rate over 8 h",
            f"{s.answers_per_day:,.0f} · ~{s.answers_per_s_average:.1f}/s",
        ),
        (
            "Decode · prefill load at the ceiling",
            f"{s.decode_tokens_per_s:,.0f} · ~{s.prefill_tokens_per_s:,.0f} tok/s",
        ),
        (
            f"GPUs at {i.gpu_target_utilisation:.0%} utilisation",
            f"{s.gpus} L4 → {s.gpu_nodes} x g6.12xlarge (N+1)",
        ),
        ("Model tokens per day", f"~{s.tokens_per_day / 1e6:.0f} M"),
        (
            "Initial index",
            f"~{s.initial_index_hours_gpu:.1f} h on one L4 "
            f"(~{s.initial_index_days_laptop:.1f} days on the laptop)",
        ),
        (
            "Daily re-index of changed documents",
            f"~{s.daily_reindex_minutes_gpu:.0f} min on one L4",
        ),
    ]
    out = ["| Input | Value | Source | Note |", "|---|---|---|---|"]
    out += [f"| {a} | {b} | {c} | {d} |" for a, b, c, d in rows_in]
    out += ["", "| Derived | Value |", "|---|---|"]
    out += [f"| {a} | {b} |" for a, b in rows_out]
    return "\n".join(out)


if __name__ == "__main__":
    print(tables())
