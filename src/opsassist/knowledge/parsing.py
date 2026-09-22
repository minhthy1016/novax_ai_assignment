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
    # Full heading path at this point in the document, outermost first, e.g.
    # ("Service playbooks", "Payment API", "Rollback"). Carried across pages.
    headings: tuple[str, ...] = ()
    level: int = 0  # heading level for kind == "heading"

    @property
    def section(self) -> str | None:
        return self.headings[-1] if self.headings else None


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


_NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+[A-Z][^.:;,!?]*$")
HeadingMode = Literal["markdown", "layout"]


def _layout_heading_level(line: str) -> int:
    """Heading heuristic for extracted PDF text, where markup is gone: a standalone short
    line without closing punctuation that is numbered ("2.1 Triage" -> level 2) or in title
    case ("Service Playbooks" -> level 1). Returns 0 when the line is not a heading."""
    text = line.strip()
    if not text or len(text) > 70 or text[-1] in ".:;,!?":
        return 0
    if m := _NUMBERED_HEADING.match(text):
        return m.group(1).count(".") + 1
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]*", text) if len(w) > 3]
    if 1 <= len(text.split()) <= 8 and words and all(w[0].isupper() for w in words):
        return 1
    return 0


class _BlockBuilder:
    """Turns lines into blocks while tracking the heading path. State persists across
    pages, so content on page 2 still knows it sits under the heading from page 1."""

    def __init__(self, mode: HeadingMode) -> None:
        self.mode = mode
        self.blocks: list[Block] = []
        self.path: list[tuple[int, str]] = []
        self._current: list[str] = []
        self._kind: BlockKind = "paragraph"
        self._page: int | None = None

    def _flush(self) -> None:
        text = " ".join(self._current).strip()
        if text:
            if (
                self.mode == "layout"
                and len(self._current) == 1
                and (level := _layout_heading_level(text))
            ):
                self._heading(level, text)
            else:
                self.blocks.append(Block(text, self._kind, self._page, self._headings()))
        self._current = []

    def _headings(self) -> tuple[str, ...]:
        return tuple(title for _, title in self.path)

    def _heading(self, level: int, title: str) -> None:
        while self.path and self.path[-1][0] >= level:
            self.path.pop()
        self.path.append((level, title))
        self.blocks.append(Block(title, "heading", self._page, self._headings(), level))

    def feed(self, lines: list[str], page: int | None = None) -> None:
        self._page = page
        for line in lines:
            if not line.strip():
                self._flush()
                continue
            if self.mode == "markdown" and (m := _HEADING.match(line)):
                self._flush()
                self._heading(len(m.group(1)), m.group(2).strip())
                continue
            if m := _ITEM.match(line):
                self._flush()
                self._kind = "item"
                self._current = [line.strip()]  # keep the number: "4. Schedule ..." is citable
                continue
            if not self._current:
                self._kind = "paragraph"
            self._current.append(line.strip())
        self._flush()


def _blocks_from_lines(
    lines: list[str], page: int | None = None, mode: HeadingMode = "markdown"
) -> list[Block]:
    builder = _BlockBuilder(mode)
    builder.feed(lines, page)
    return builder.blocks


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
        builder = _BlockBuilder("layout")
        pages = []
        for number, page in enumerate(reader.pages, start=1):
            # Layout mode keeps the vertical gaps between paragraphs as blank lines, so
            # paragraph boundaries (and therefore locators) survive extraction.
            text = normalize(page.extract_text(extraction_mode="layout") or "")
            pages.append(text)
            builder.feed(text.split("\n"), page=number)
        blocks = builder.blocks
        mime, raw = "application/pdf", "\n\n".join(pages)

    try:
        meta = DocumentMeta.model_validate(raw_meta)
    except ValueError as exc:
        raise ParseError(f"{path.name}: invalid metadata: {exc}") from exc
    if not any(b.kind != "heading" for b in blocks):
        raise ParseError(f"{path.name}: no text content extracted")
    return ParsedDocument(meta, blocks, mime, str(path), raw)
