"""Render a plain-text file into a minimal, dependency-free, multi-page PDF.

Usage: python scripts/make_sample_pdf.py SOURCE.txt OUTPUT.pdf

* Paragraphs are blank-line separated and wrapped at ~90 characters.
* A paragraph starting with "# " is rendered as a bold heading (without the "#"), so the PDF
  has real visual structure for parsers and layout models to recover.
* Pages break automatically; a heading is never left alone at the bottom of a page.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

PAGE_W, PAGE_H = 612, 842
TOP, BOTTOM, LEFT = 780, 60, 56
BODY, BODY_LEAD = 11, 14
HEAD, HEAD_LEAD = 14, 22


def _escape(line: str) -> str:
    return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _layout(text: str) -> list[list[tuple[str, str]]]:
    """Returns pages of (font, line) where font is 'H' (heading), 'B' (body) or '' (gap)."""
    items: list[tuple[str, str]] = []
    for para in text.strip().split("\n\n"):
        para = " ".join(para.split())
        if para.startswith("# "):
            items += [("", ""), ("H", para[2:]), ("", "")]
        else:
            items += [("B", line) for line in textwrap.wrap(para, 90)] + [("", "")]
    pages: list[list[tuple[str, str]]] = [[]]
    y = TOP
    for i, (font, line) in enumerate(items):
        lead = HEAD_LEAD if font == "H" else BODY_LEAD
        keep_with_next = font == "H" and i + 2 < len(items)
        needed = lead + (3 * BODY_LEAD if keep_with_next else 0)
        if y - needed < BOTTOM and pages[-1]:
            pages.append([])
            y = TOP
        if font == "" and not pages[-1]:
            continue  # no leading gap at the top of a page
        pages[-1].append((font, line))
        y -= lead
    return pages


def _content(page: list[tuple[str, str]]) -> bytes:
    ops = ["BT", f"{LEFT} {TOP} Td"]
    for font, line in page:
        if font == "H":
            ops += [f"/F2 {HEAD} Tf", f"0 -{HEAD_LEAD - BODY_LEAD} Td", f"({_escape(line)}) Tj"]
            ops += [f"0 -{BODY_LEAD} Td"]
        else:
            ops += [f"/F1 {BODY} Tf", f"({_escape(line)}) Tj", f"0 -{BODY_LEAD} Td"]
    ops.append("ET")
    return "\n".join(ops).encode("latin-1")


def render(text: str) -> bytes:
    pages = _layout(text)
    n = len(pages)
    # Object numbers: 1 catalog, 2 pages, 3 regular font, 4 bold font,
    # then for each page: page object and its content stream.
    kids = " ".join(f"{5 + 2 * i} 0 R" for i in range(n))
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
    ]
    for i, page in enumerate(pages):
        stream = _content(page)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
            "/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
            f"/Contents {6 + 2 * i} 0 R >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


if __name__ == "__main__":
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    dst.write_bytes(render(src.read_text(encoding="utf-8")))
    print(f"wrote {dst} ({dst.stat().st_size} bytes)")
