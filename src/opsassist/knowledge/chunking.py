"""Structure-aware chunking with human-readable locators.

Parameters (justified in architecture.md D-20):

* **target ~64 tokens, hard max 160.** The knowledge base is short operational documents
  (~90 tokens on average) whose answers are single facts ("Tuesday or Thursday,
  21:00-23:00 MYT"). At a 120-token target every sample document collapsed into one chunk
  and citations could only say "¶1-7"; ~64 tokens (one to three items or sentences) keeps
  each citation close to the exact claim while top-K=4 still fits in ~300 tokens of
  context. The value is measured against 120 and whole-document chunking in the eval.
* **Boundaries follow structure:** list items and paragraphs are never split unless a single
  one exceeds the max; a new heading always starts a new chunk; chunks never merge sections.
* **Overlap only where it helps:** when an oversized paragraph must be split, consecutive
  pieces share one sentence so a fact straddling the cut is not lost. Whole items and
  paragraphs need no overlap - they are self-contained units.
* **Contextual header:** the text that is embedded and full-text indexed is prefixed with
  the document title and section, so a chunk like "Roll back if error rate exceeds 2%" is
  still retrievable by "deployment rollback". The stored display text is the body only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from opsassist.knowledge.parsing import Block, ParsedDocument
from opsassist.providers.base import estimate_tokens

DASH = "\u2013"  # en dash for ranges in human-readable locators, e.g. "¶1{DASH}4"
TARGET_TOKENS = 64
MAX_TOKENS = 160
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


@dataclass(frozen=True, slots=True)
class Unit:
    text: str
    ordinal: int  # 1-based paragraph/item number within the document
    page: int | None
    section: str | None


@dataclass(frozen=True, slots=True)
class Chunk:
    index: int
    text: str
    embed_text: str
    locator: str
    section: str | None
    page_start: int | None
    token_count: int


def _units(blocks: list[Block]) -> list[Unit]:
    units: list[Unit] = []
    ordinal = 0
    for block in blocks:
        if block.kind == "heading":
            continue
        ordinal += 1
        if estimate_tokens(block.text) <= MAX_TOKENS:
            units.append(Unit(block.text, ordinal, block.page, block.section))
            continue
        # Oversized paragraph: split at sentence boundaries, one sentence of overlap.
        sentences = _SENTENCE.split(block.text)
        piece: list[str] = []
        for sentence in sentences:
            if piece and estimate_tokens(" ".join([*piece, sentence])) > TARGET_TOKENS:
                units.append(Unit(" ".join(piece), ordinal, block.page, block.section))
                piece = [piece[-1]]
            piece.append(sentence)
        if piece:
            units.append(Unit(" ".join(piece), ordinal, block.page, block.section))
    return units


def _locator(group: list[Unit]) -> str:
    first, last = group[0], group[-1]
    paras = (
        f"¶{first.ordinal}"
        if first.ordinal == last.ordinal
        else f"¶{first.ordinal}{DASH}{last.ordinal}"
    )
    parts = []
    if first.section:
        parts.append(f"§{first.section}")
    if first.page is not None:
        pages = (
            f"p.{first.page}" if first.page == last.page else f"pp.{first.page}{DASH}{last.page}"
        )
        parts.append(pages)
    parts.append(paras)
    return " ".join(parts)


def chunk_document(doc: ParsedDocument) -> list[Chunk]:
    groups: list[list[Unit]] = []
    current: list[Unit] = []
    for unit in _units(doc.blocks):
        starts_new_section = bool(current) and unit.section != current[-1].section
        too_big = bool(current) and (
            estimate_tokens(" ".join(u.text for u in [*current, unit])) > TARGET_TOKENS
        )
        if starts_new_section or too_big:
            groups.append(current)
            current = []
        current.append(unit)
    if current:
        groups.append(current)

    chunks = []
    for index, group in enumerate(groups):
        body = "\n".join(u.text for u in group)
        header = (
            doc.meta.title if not group[0].section else f"{doc.meta.title} - {group[0].section}"
        )
        chunks.append(
            Chunk(
                index=index,
                text=body,
                embed_text=f"{header}\n{body}",
                locator=_locator(group),
                section=group[0].section,
                page_start=group[0].page,
                token_count=estimate_tokens(body),
            )
        )
    return chunks
