"""The scale proposal quotes the capacity model, not numbers typed by hand."""

from __future__ import annotations

import re
from pathlib import Path

from evaluation.capacity import Inputs, size, tables

D60 = Path(__file__).resolve().parents[2] / "docs" / "decisions" / "D-60-scale-proposal-aws.md"


def test_d60_sizing_tables_are_the_capacity_model_output() -> None:
    text = D60.read_text(encoding="utf-8")
    block = re.search(r"<!-- capacity:begin -->\n(.*?)\n<!-- capacity:end -->", text, re.S)
    assert block, "D-60 must keep its sizing tables between the capacity markers"
    assert block.group(1) == tables(), "D-60 is stale: run `python -m evaluation.capacity`"


def test_sizing_follows_from_its_inputs() -> None:
    base = size()
    # Twice the document length is twice the chunks and index, nothing else moves.
    longer = size(Inputs(avg_document_tokens=5_000))
    assert round(longer.children / base.children, 6) == 2.0
    assert longer.gpus == base.gpus
    # The ceiling is Little's law: in-flight requests / seconds per answer.
    assert base.answers_per_s_at_ceiling == 100 / base.answer_seconds
    # Always one node more than the GPUs need, so losing a node or an AZ keeps serving.
    assert base.gpu_nodes * Inputs().gpus_per_node >= base.gpus + Inputs().gpus_per_node
