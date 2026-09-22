"""Parse Markdown, plain text and PDF into normalized, structure-tagged blocks.

Metadata is mandatory and validated: Markdown carries it as front matter; ``.txt`` and
``.pdf`` files carry it in a sidecar ``<name>.meta.json``. A document without a valid
department and classification is rejected - it must never be indexed as "no ACL".
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pypdf import PdfReader

Classification = Literal["public", "internal", "confidential"]
BlockKind = Literal["heading", "paragraph", "item"]
SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt", ".pdf"}


class DocumentMeta(BaseModel):
    document_id: str = Field(pattern=r"^KB-[A-Z]+-[0-9]{3}$")
    title: str = Field(min_length=1, max_length=300)
    department: str = Field(pattern=r"^[a-z][a-z_]*$")
    classification: Classification
    updated_at: date
    source: str | None = None  # e.g. "brief" or "candidate-added"


@dataclass(frozen=True, slots=True)
class Block:
    text: str
    kind: BlockKind
    page: int | None = None
    section: str | None = None


@dataclass
class ParsedDocument:
    meta: DocumentMeta
    blocks: list[Block]
    mime_type: str
    source_path: str
    raw_text: str = field(repr=False, default="")


class ParseError(ValueError):
    pass


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES = re.compile("[ \t\u00a0]+")
_ITEM = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+(.*)$")
_HEADING = re.compile(r"^\s*(#{1,6})\s+(.*)$")


def normalize(text: str) -> str:
    """NFKC, strip control characters, collapse runs of spaces. Newlines are kept for
    structure detection and collapsed later per block."""
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    return "\n".join(_SPACES.sub(" ", line).strip() for line in text.split("\n"))


def _blocks_from_lines(lines: list[str], page: int | None = None) -> list[Block]:
    """Headings, list items and blank-line separated paragraphs. Continuation lines are
    joined to the block they belong to."""
    blocks: list[Block] = []
    section: str | None = None
    current: list[str] = []
    current_kind: BlockKind = "paragraph"

    def flush() -> None:
        nonlocal current
        text = " ".join(current).strip()
        if text:
            blocks.append(Block(text, current_kind, page, section))
        current = []

    for line in lines:
        if not line.strip():
            flush()
            continue
        if m := _HEADING.match(line):
            flush()
            section = m.group(2).strip()
            blocks.append(Block(section, "heading", page, section))
            continue
        if m := _ITEM.match(line):
            flush()
            current_kind = "item"
            current = [line.strip()]  # keep the number: "4. Schedule ..." is citable
            continue
        if not current:
            current_kind = "paragraph"
        current.append(line.strip())
    flush()
    return blocks


def _split_front_matter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        raise ParseError("markdown document is missing front matter")
    end = text.find("\n---", 4)
    if end == -1:
        raise ParseError("unterminated front matter")
    meta: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise ParseError(f"malformed front matter line: {line!r}")
        meta[key.strip()] = value.strip().strip('"')
    return meta, text[end + 4 :]


def _sidecar(path: Path) -> dict[str, str]:
    meta_path = path.with_name(path.name + ".meta.json")
    if not meta_path.exists():
        raise ParseError(f"{path.name}: missing metadata sidecar {meta_path.name}")
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ParseError(f"{meta_path.name}: metadata must be a JSON object")
    return {str(k): str(v) for k, v in data.items()}


def parse_file(path: Path) -> ParsedDocument:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ParseError(f"unsupported file type {suffix!r}")

    if suffix in (".md", ".markdown"):
        raw_meta, body = _split_front_matter(normalize(path.read_text(encoding="utf-8")))
        blocks = _blocks_from_lines(body.split("\n"))
        mime, raw = "text/markdown", body
    elif suffix == ".txt":
        raw_meta = _sidecar(path)
        raw = normalize(path.read_text(encoding="utf-8"))
        blocks = _blocks_from_lines(raw.split("\n"))
        mime = "text/plain"
    else:
        raw_meta = _sidecar(path)
        reader = PdfReader(str(path))
        blocks, pages = [], []
        for number, page in enumerate(reader.pages, start=1):
            # Layout mode keeps the vertical gaps between paragraphs as blank lines, so
            # paragraph boundaries (and therefore locators) survive extraction.
            text = normalize(page.extract_text(extraction_mode="layout") or "")
            pages.append(text)
            blocks.extend(_blocks_from_lines(text.split("\n"), page=number))
        mime, raw = "application/pdf", "\n\n".join(pages)

    try:
        meta = DocumentMeta.model_validate(raw_meta)
    except ValueError as exc:
        raise ParseError(f"{path.name}: invalid metadata: {exc}") from exc
    if not any(b.kind != "heading" for b in blocks):
        raise ParseError(f"{path.name}: no text content extracted")
    return ParsedDocument(meta, blocks, mime, str(path), raw)
