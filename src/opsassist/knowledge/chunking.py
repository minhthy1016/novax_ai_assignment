"""Chunking strategies with citable locators.

Three strategies share one output type, so ingestion, retrieval and the chunking eval
(``evaluation/chunking_eval.py``) can swap them freely:

``structural``
    The original day-3 strategy: paragraphs/list items packed to a small target (~64
    tokens); header = title + nearest heading. Precise citations, but short chunks lose
    context - kept as the evaluation baseline.

``hierarchical``
    Docling-HybridChunker-style. A chunk never spans two sections; a section is packed
    into as few chunks as fit ``max_tokens`` (oversized paragraphs are split at sentences
    with one sentence of overlap); the *full heading path* ("Service playbooks > Payment
    API") is prepended to the embedded text. Facts that are only unambiguous under their
    heading (90% vs 80% escalation thresholds) keep that heading.

``parent_child`` ("small-to-big")
    Match on small structural chunks (precise embeddings), but hand the model the whole
    hierarchical section that contains the match (``context``), so the answer has the
    surrounding facts. Citations point at the section.

The choice between them is made from measured recall@k - see architecture.md D-20.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from opsassist.knowledge.parsing import Block, ParsedDocument
from opsassist.providers.base import estimate_tokens

Strategy = Literal["structural", "hierarchical", "parent_child"]
DASH = "\u2013"  # en dash for ranges in locators, e.g. paragraphs 1 to 4
PATH_SEP = " > "  # heading path in embedded text
LOCATOR_SEP = " \u203a "  # single right angle quote between headings in locators
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


@dataclass(frozen=True, slots=True)
class ChunkingConfig:
    strategy: Strategy = "parent_child"  # same default as Settings.chunking_strategy
    target_tokens: int = 64  # structural chunks / parent_child children
    max_tokens: int = 256  # hierarchical sections / parents
    split_tokens: int = 160  # a single paragraph above this is split at sentences


@dataclass(frozen=True, slots=True)
class Unit:
    text: str
    ordinal: int  # 1-based paragraph/item number within the document
    page: int | None
    headings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Chunk:
    index: int
    text: str  # the matched text
    embed_text: str  # what is embedded and full-text indexed
    context: str  # what the model receives; equals text except for parent_child
    locator: str
    section: str | None
    headings: tuple[str, ...]
    page_start: int | None
    token_count: int


def _units(blocks: list[Block], split_tokens: int, target_tokens: int) -> list[Unit]:
    units: list[Unit] = []
    ordinal = 0
    for block in blocks:
        if block.kind == "heading":
            continue
        ordinal += 1
        if estimate_tokens(block.text) <= split_tokens:
            units.append(Unit(block.text, ordinal, block.page, block.headings))
            continue
        # Oversized paragraph: split at sentence boundaries, one sentence of overlap.
        piece: list[str] = []
        for sentence in _SENTENCE.split(block.text):
            if piece and estimate_tokens(" ".join([*piece, sentence])) > target_tokens:
                units.append(Unit(" ".join(piece), ordinal, block.page, block.headings))
                piece = [piece[-1]]
            piece.append(sentence)
        if piece:
            units.append(Unit(" ".join(piece), ordinal, block.page, block.headings))
    return units


def _pack(units: list[Unit], limit: int) -> list[list[Unit]]:
    """Greedy packing that never crosses a section (heading path) boundary."""
    groups: list[list[Unit]] = []
    current: list[Unit] = []
    for unit in units:
        new_section = bool(current) and unit.headings != current[-1].headings
        too_big = bool(current) and (
            estimate_tokens(" ".join(u.text for u in [*current, unit])) > limit
        )
        if new_section or too_big:
            groups.append(current)
            current = []
        current.append(unit)
    if current:
        groups.append(current)
    return groups


def _path(title: str, headings: tuple[str, ...]) -> list[str]:
    # The document title is often also the first heading; don't repeat it.
    return [h for h in headings if h != title]


def _locator(title: str, group: list[Unit], full_path: bool) -> str:
    first, last = group[0], group[-1]
    paras = (
        f"¶{first.ordinal}"
        if first.ordinal == last.ordinal
        else f"¶{first.ordinal}{DASH}{last.ordinal}"
    )
    parts = []
    path = _path(title, first.headings)
    if path:
        parts.append("§" + (LOCATOR_SEP.join(path) if full_path else path[-1]))
    if first.page is not None:
        pages = (
            f"p.{first.page}" if first.page == last.page else f"pp.{first.page}{DASH}{last.page}"
        )
        parts.append(pages)
    parts.append(paras)
    return " ".join(parts)


def _header(title: str, headings: tuple[str, ...], full_path: bool) -> str:
    path = _path(title, headings)
    if not path:
        return title
    return f"{title}\n{PATH_SEP.join(path)}" if full_path else f"{title} - {path[-1]}"


def _body(group: list[Unit]) -> str:
    return "\n".join(u.text for u in group)


def _make(index: int, group: list[Unit], context: str, header: str, locator: str) -> Chunk:
    body = _body(group)
    return Chunk(
        index=index,
        text=body,
        embed_text=f"{header}\n{body}",
        context=context,
        locator=locator,
        section=group[0].headings[-1] if group[0].headings else None,
        headings=group[0].headings,
        page_start=group[0].page,
        token_count=estimate_tokens(body),
    )


def chunk_document(doc: ParsedDocument, config: ChunkingConfig | None = None) -> list[Chunk]:
    cfg = config or ChunkingConfig()
    title = doc.meta.title
    units = _units(doc.blocks, cfg.split_tokens, cfg.target_tokens)

    if cfg.strategy == "structural":
        return [
            _make(i, g, _body(g), _header(title, g[0].headings, False), _locator(title, g, False))
            for i, g in enumerate(_pack(units, cfg.target_tokens))
        ]

    sections = _pack(units, cfg.max_tokens)
    if cfg.strategy == "hierarchical":
        return [
            _make(i, g, _body(g), _header(title, g[0].headings, True), _locator(title, g, True))
            for i, g in enumerate(sections)
        ]

    # parent_child: small children for matching, each carrying its whole section as context.
    chunks: list[Chunk] = []
    for parent in sections:
        parent_text, parent_locator = _body(parent), _locator(title, parent, True)
        for child in _pack(parent, cfg.target_tokens):
            header = _header(title, child[0].headings, True)
            chunks.append(_make(len(chunks), child, parent_text, header, parent_locator))
    return chunks
