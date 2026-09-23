"""Render a deliberately awkward PDF: two-column prose, ruled tables, running headers.

The simple generator (`make_sample_pdf.py`) produces one column of flowing text, which every
chunker handles. A layout model earns its keep on the documents this one produces:

* **Two columns.** Naive text extraction reads across the gutter and glues unrelated
  sentences together; the facts then straddle a chunk boundary that should not exist.
* **Ruled tables.** A fact lives in a cell — the row label is on the left, the value three
  columns over, and nothing but geometry connects them.
* **Running headers and footers.** Repeated text that belongs to no section and should not
  be embedded with the content around it.
* **A continued table across a page break**, where the header row appears once.

Everything in it is fictional. Usage:

    python scripts/make_complex_pdf.py sample_data/knowledge/KB-ENG-005.pdf
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

PAGE_W, PAGE_H = 612, 792
MARGIN, TOP, BOTTOM = 54, 720, 66
COL_GAP = 24
COL_W = (PAGE_W - 2 * MARGIN - COL_GAP) / 2
BODY, LEAD = 9.5, 12.5

TITLE = "Service Capacity and On-Call Rate Card 2026"
HEADER = "ACME INTERNAL - ENGINEERING OPERATIONS"
FOOTER = "Confidentiality: Internal. Document KB-ENG-005. Page {page}"

INTRO = """This rate card records the capacity limits each Engineering service is funded \
for in 2026, the thresholds at which it must be scaled, and the on-call cover that applies \
outside working hours. It is reviewed every quarter by the Engineering Manager together \
with Finance. Where a service exceeds its funded ceiling for two consecutive weeks, the \
owning team raises a capacity request rather than scaling silently.

Capacity figures are steady-state ceilings, not peak survivability: a service may briefly \
exceed its ceiling during an incident or a planned migration. Sustained breach is what the \
review looks at. The p95 latency target in the table is a service objective, not an alerting \
threshold; alerting fires at 1.5 times the target for ten consecutive minutes.

On-call cover is organised in two tiers. Tier 1 is the owning team's own rota and answers \
first for its services. Tier 2 is the platform rota and is paged only when Tier 1 does not \
acknowledge within the stated window, or when an incident spans more than one service. \
Paging Tier 2 directly is permitted for SEV1 incidents only.

Cost recovery is internal: each team is charged the monthly rate shown for the tier it \
subscribes to, and the charge does not change with the number of pages received. A team \
that cancels Tier 2 cover keeps Tier 1 and loses cross-service escalation."""

CAPACITY_ROWS = [
    ("Service", "Funded RPS", "p95 target", "Scale trigger"),
    ("checkout-api", "1,200", "180 ms", "sustained 70% for 2 weeks"),
    ("payment-worker", "400", "900 ms", "queue depth above 5,000"),
    ("search-api", "2,500", "250 ms", "cluster CPU above 65%"),
    ("catalogue-api", "3,000", "120 ms", "sustained 75% for 2 weeks"),
    ("notify-worker", "150", "2,000 ms", "queue depth above 20,000"),
    ("reporting-batch", "n/a", "n/a", "run time above 90 minutes"),
]

ONCALL_ROWS = [
    ("Tier", "Hours", "Acknowledge within", "Monthly rate (MYR)"),
    ("Tier 1 - owning team", "18:00-08:00 and weekends", "15 minutes", "4,800"),
    ("Tier 2 - platform", "24/7", "30 minutes", "9,600"),
    ("Tier 2 - platform (SEV1)", "24/7", "10 minutes", "included in Tier 2"),
]

NOTES = """Escalation to the database on-call engineer is charged to the platform rota, \
not to the owning team, because the database tier is shared. A team may request a temporary \
uplift of its funded RPS for a launch; uplifts are approved for at most 30 days and revert \
automatically.

Reporting-batch has no funded RPS because it is a scheduled job rather than a request \
service. Its capacity is measured by run time: a run exceeding 90 minutes triggers a review \
of the query plan before any additional workers are funded.

Services not listed here are unfunded and run on best-effort shared capacity. An unfunded \
service may not declare a SEV1: it must first be added to this rate card at the quarterly \
review."""


def esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class Page:
    """A PDF content stream being built up, in points from the bottom-left corner."""

    def __init__(self, number: int) -> None:
        self.number = number
        self.ops: list[str] = []
        self.header()

    def text(self, x: float, y: float, s: str, size: float = BODY, bold: bool = False) -> None:
        font = "/F2" if bold else "/F1"
        self.ops.append(f"BT {font} {size} Tf 1 0 0 1 {x:.1f} {y:.1f} Tm ({esc(s)}) Tj ET")

    def line(self, x0: float, y0: float, x1: float, y1: float, width: float = 0.5) -> None:
        self.ops.append(f"{width} w {x0:.1f} {y0:.1f} m {x1:.1f} {y1:.1f} l S")

    def header(self) -> None:
        self.text(MARGIN, PAGE_H - 40, HEADER, 8, bold=True)
        self.line(MARGIN, PAGE_H - 46, PAGE_W - MARGIN, PAGE_H - 46, 0.4)
        self.line(MARGIN, BOTTOM - 8, PAGE_W - MARGIN, BOTTOM - 8, 0.4)
        self.text(MARGIN, BOTTOM - 20, FOOTER.format(page=self.number), 8)

    def stream(self) -> str:
        return "\n".join(self.ops)


def two_columns(pages: list[Page], page: Page, text: str, y: float) -> tuple[Page, float]:
    """Flow a block of prose down the left column, then the right, then onto a new page."""
    lines = [line for para in text.split("\n\n") for line in (*textwrap.wrap(para, 52), "")]
    column, y_start = 0, y
    for line in lines:
        x = MARGIN + column * (COL_W + COL_GAP)
        if y < BOTTOM + LEAD:
            if column == 0:
                column, y = 1, y_start
                x = MARGIN + COL_W + COL_GAP
            else:
                page = Page(page.number + 1)
                pages.append(page)
                column, y, y_start = 0, TOP, TOP
                x = MARGIN
        if line:
            page.text(x, y, line)
        y -= LEAD
    return page, y


def table(
    pages: list[Page], page: Page, title: str, rows: list[tuple[str, ...]], y: float
) -> tuple[Page, float]:
    widths = [150, 90, 110, 154]
    if y < BOTTOM + 90:  # never start a table at the very bottom
        page = Page(page.number + 1)
        pages.append(page)
        y = TOP
    page.text(MARGIN, y, title, 11, bold=True)
    y -= 18
    for index, row in enumerate(rows):
        if y < BOTTOM + 24:
            page = Page(page.number + 1)
            pages.append(page)
            y = TOP
            # The header row is deliberately NOT repeated: a continued table is exactly the
            # case where a layout model and a text dump disagree.
        x = MARGIN
        for cell, width in zip(row, widths, strict=False):
            page.text(x + 3, y, cell, BODY, bold=index == 0)
            x += width
        y -= 6
        page.line(MARGIN, y, MARGIN + sum(widths), y, 0.8 if index == 0 else 0.3)
        y -= 12
    return page, y - 8


def build() -> bytes:
    pages = [Page(1)]
    page = pages[0]
    page.text(MARGIN, TOP, TITLE, 16, bold=True)
    y = TOP - 26
    page.text(MARGIN, y, "Engineering Operations - reviewed quarterly - effective 1 July 2026", 9)
    y -= 24
    page, y = table(pages, page, "Table 1. Funded capacity by service", CAPACITY_ROWS, y)
    page, y = two_columns(pages, page, INTRO, y - 6)
    page, y = table(pages, page, "Table 2. On-call tiers and charges", ONCALL_ROWS, y)
    page, y = two_columns(pages, page, NOTES, y - 6)
    return assemble(pages)


def assemble(pages: list[Page]) -> bytes:
    """Minimal PDF writer: catalog, page tree, two fonts, one content stream per page."""
    objects: list[bytes] = []

    def add(body: str | bytes) -> int:
        objects.append(body.encode("latin-1") if isinstance(body, str) else body)
        return len(objects)

    font_regular = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    font_bold = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
    pages_id = len(objects) + 1 + 2 * len(pages) + 1  # page objects + streams, then the tree
    kids = []
    for page in pages:
        stream = page.stream().encode("latin-1")
        content_id = add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
        kids.append(
            add(
                f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                f"/Resources << /Font << /F1 {font_regular} 0 R /F2 {font_bold} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            )
        )
    kid_refs = " ".join(f"{k} 0 R" for k in kids)
    tree = add(f"<< /Type /Pages /Count {len(kids)} /Kids [{kid_refs}] >>")
    catalog = add(f"<< /Type /Catalog /Pages {tree} 0 R >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % index + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets[1:]:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        catalog,
        start,
    )
    return bytes(out)


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "sample_data/knowledge/KB-ENG-005.pdf")
    target.write_bytes(build())
    print(f"wrote {target} ({target.stat().st_size / 1024:.0f} KB)")
