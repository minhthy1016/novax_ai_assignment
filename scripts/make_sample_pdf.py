"""Render a plain-text file into a minimal, dependency-free PDF (for the PDF sample).

Usage: python scripts/make_sample_pdf.py SOURCE.txt OUTPUT.pdf

Each paragraph (blank-line separated) is wrapped at ~90 characters; paragraphs are
separated by an empty line so text extraction keeps paragraph boundaries.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path


def _escape(line: str) -> str:
    return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def render(text: str) -> bytes:
    lines: list[str] = []
    for para in text.strip().split("\n\n"):
        lines.extend(textwrap.wrap(" ".join(para.split()), 90) or [""])
        lines.append("")
    ops = ["BT", "/F1 11 Tf", "14 TL", "56 780 Td"]
    ops += [f"({_escape(line)}) Tj T*" for line in lines]
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
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
