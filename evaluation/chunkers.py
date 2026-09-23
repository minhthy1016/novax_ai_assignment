"""Alternative chunking strategies, kept for evaluation only.

Production uses parent-child (``opsassist.knowledge.chunking.chunk_document``). These are
the strategies it was measured against in ``evaluation/chunking_eval.py``; they reuse the
production building blocks so the comparison differs only in the strategy itself.
"""

from __future__ import annotations

from opsassist.knowledge.chunking import (
    Chunk,
    body_of,
    header_for,
    locator_for,
    make_chunk,
    pack_units,
    split_units,
)
from opsassist.knowledge.parsing import ParsedDocument


def structural(
    doc: ParsedDocument, target_tokens: int = 64, split_tokens: int = 160
) -> list[Chunk]:
    """The first (day-3) strategy: small packs of paragraphs, nearest heading only."""
    title = doc.meta.title
    groups = pack_units(split_units(doc.blocks, split_tokens, target_tokens), target_tokens)
    return [
        make_chunk(
            i, g, body_of(g), header_for(title, g[0].headings, False), locator_for(title, g, False)
        )
        for i, g in enumerate(groups)
    ]


def hierarchical(
    doc: ParsedDocument, max_tokens: int = 256, split_tokens: int = 160
) -> list[Chunk]:
    """Docling-HybridChunker-style: one chunk per section (split if too big), full path."""
    title = doc.meta.title
    groups = pack_units(split_units(doc.blocks, split_tokens, 64), max_tokens)
    return [
        make_chunk(i, g, body_of(g), header_for(title, g[0].headings), locator_for(title, g))
        for i, g in enumerate(groups)
    ]
