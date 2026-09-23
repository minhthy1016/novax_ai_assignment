"""The console: a single static page for exercising the whole pipeline by hand.

It is a *client*, exactly like Flowise (D-06) and like `curl`: it holds no policy and no
credentials, and every rule it appears to demonstrate - who may read a document, which tool
a caller may run, who may approve it - is enforced by the API it calls. Deleting this file
changes nothing about what the system permits.

It is mounted only in dev/test, because it signs in through the development token issuer,
which does not exist elsewhere either.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Response
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["ui"])

CONSOLE = Path(__file__).resolve().parents[3] / "web" / "index.html"

# The page is self-contained: no CDN, no inline event handlers, nothing fetched from
# anywhere but this origin. The policy says so as well, so a future edit that pulls in a
# script from the internet fails in the browser rather than quietly shipping.
_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data:; connect-src 'self'; form-action 'none'; frame-ancestors 'none'"
)


@router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
async def console() -> Response:
    if not CONSOLE.is_file():  # running from a wheel without the page
        return HTMLResponse("<h1>console not bundled</h1>", status_code=404)
    return HTMLResponse(
        CONSOLE.read_text(encoding="utf-8"),
        headers={
            "Content-Security-Policy": _CSP,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
