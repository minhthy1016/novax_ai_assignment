"""Parent-child ("small-to-big") chunking with citable locators.

* **Children** (~64 tokens) are what gets embedded and matched: small chunks keep the
  embedding focused on one fact.
* **Parents** are sections: consecutive paragraphs under the same heading path, packed up to
  ~256 tokens and never crossing a heading. The model receives the parent (``context``), so
  an answer sees the surrounding facts, and the citation names the section.
* The **full heading path** ("Service playbooks > Payment API") is prepended to what is
  embedded, so facts that are only unambiguous under their heading keep it.
* An oversized paragraph is split at sentence boundaries with one sentence of overlap.

This strategy was chosen over structural, hierarchical, fixed-window, per-page and Docling's
HybridChunker by measured recall@k (architecture.md D-20). The alternatives live in
``evaluation/chunkers.py`` and reuse the building blocks below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from opsassist.knowledge.parsing import Block, ParsedDocument
from opsassist.providers.base import estimate_tokens

DASH = "\u2013"  # en dash for ranges in locators, e.g. paragraphs 1 to 4
PATH_SEP = " > "  # heading path in embedded text
LOCATOR_SEP = " \u203a "  # single right angle quote between headings in locators
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


@dataclass(frozen=True, slots=True)
class ChunkingConfig:
    target_tokens: int = 64  # children (what is embedded and matched)
    max_tokens: int = 256  # parents (sections handed to the model)
    split_tokens: int = 160  # a single paragraph above this is split at sentences

    @property
    def chunker_id(self) -> str:
        """Recorded per document version and part of the content hash: changing the
        chunking re-indexes documents instead of mixing chunkings in one index."""
        return f"parent_child:{self.target_tokens}/{self.max_tokens}/{self.split_tokens}"


@dataclass(frozen=True, slots=True)
class Unit:
    text: str
    ordinal: int  # 1-based paragraph/item number within the document
    page: int | None
    headings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Chunk:
    index: int
    text: str  # the matched text (child)
    embed_text: str  # what is embedded and full-text indexed
    context: str  # what the model receives (parent section)
    locator: str
    section: str | None
    headings: tuple[str, ...]
    page_start: int | None
    token_count: int


# ------------------------------------------------------------------ building blocks


def split_units(blocks: list[Block], split_tokens: int, target_tokens: int) -> list[Unit]:
    """Paragraphs and list items as units; oversized ones split at sentences with overlap."""
    units: list[Unit] = []
    ordinal = 0
    for block in blocks:
        if block.kind == "heading":
            continue
        ordinal += 1
        if estimate_tokens(block.text) <= split_tokens:
            units.append(Unit(block.text, ordinal, block.page, block.headings))
            continue
        piece: list[str] = []
        for sentence in _SENTENCE.split(block.text):
            if piece and estimate_tokens(" ".join([*piece, sentence])) > target_tokens:
                units.append(Unit(" ".join(piece), ordinal, block.page, block.headings))
                piece = [piece[-1]]
            piece.append(sentence)
        if piece:
            units.append(Unit(" ".join(piece), ordinal, block.page, block.headings))
    return units


def pack_units(units: list[Unit], limit: int) -> list[list[Unit]]:
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


def locator_for(title: str, group: list[Unit], full_path: bool = True) -> str:
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


def header_for(title: str, headings: tuple[str, ...], full_path: bool = True) -> str:
    path = _path(title, headings)
    if not path:
        return title
    return f"{title}\n{PATH_SEP.join(path)}" if full_path else f"{title} - {path[-1]}"


def body_of(group: list[Unit]) -> str:
    return "\n".join(u.text for u in group)


def make_chunk(index: int, group: list[Unit], context: str, header: str, locator: str) -> Chunk:
    body = body_of(group)
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


# ------------------------------------------------------------------ production chunker


def chunk_document(doc: ParsedDocument, config: ChunkingConfig | None = None) -> list[Chunk]:
    cfg = config or ChunkingConfig()
    title = doc.meta.title
    units = split_units(doc.blocks, cfg.split_tokens, cfg.target_tokens)
    chunks: list[Chunk] = []
    for parent in pack_units(units, cfg.max_tokens):
        context, locator = body_of(parent), locator_for(title, parent)
        for child in pack_units(parent, cfg.target_tokens):
            header = header_for(title, child[0].headings)
            chunks.append(make_chunk(len(chunks), child, context, header, locator))
    return chunks
