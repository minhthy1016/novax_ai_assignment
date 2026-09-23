"""Document upload by authorized users.

An upload is a *write* into the knowledge base, so the uploader becomes a content source.
The threats this guards against (architecture.md D-27):

1. **Privilege escalation through metadata.** Metadata inside an uploaded file is untrusted.
   The department is taken from the uploader's `kb:write:<department>` permission, never from
   the file; a file claiming a different department is rejected rather than silently fixed.
2. **Classification downgrade.** The default is the strictest class the uploader may write.
   `confidential` requires `<department>:confidential`; `public` requires a publish
   permission that nobody holds in the sample data, so no user can make a document
   company-wide.
3. **Indirect prompt injection.** Unchanged from any other document: text is escaped data
   with no authority, and tools are authorized outside the model.
4. **Knowledge poisoning.** Not preventable when the user legitimately owns the department,
   so it is made visible instead: provenance (`uploaded_by`) is stored and audited, and the
   document can be deleted or rolled back to a previous version.
5. **Hostile or leaky files.** Size and type limits, filenames sanitized, no execution, and a
   secret scan that rejects files containing credential-looking strings.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from opsassist.auth import Principal
from opsassist.db.models import Document
from opsassist.knowledge.parsing import SUPPORTED_SUFFIXES, Classification

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
UPLOAD_SUBDIR = "uploads"
WRITE_PERMISSION = re.compile(r"^kb:write:([a-z][a-z_]*)$")
PUBLISH_PERMISSION = "kb:publish:public"

# Credential-shaped strings; an uploaded document must not carry secrets into the index.
SECRET_PATTERNS = [
    re.compile(r"\b(sk-ant|sk-|nvapi-|ghp_|xoxb-)[A-Za-z0-9_\-]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(password|passwd|secret|api[_-]?key)\s*[:=]\s*\S{6,}", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]


class UploadRejected(ValueError):
    """The upload is not allowed or not acceptable; never partially stored."""


@dataclass(frozen=True)
class UploadPlan:
    doc_key: str
    department: str
    classification: Classification
    title: str
    path: Path


def writable_departments(principal: Principal) -> set[str]:
    return {
        m.group(1) for p in principal.permissions if (m := WRITE_PERMISSION.match(p)) is not None
    }


def choose_classification(
    principal: Principal, department: str, requested: str | None
) -> Classification:
    if requested in (None, "internal"):
        return "internal"
    if requested == "confidential":
        if not principal.has(f"{department}:confidential"):
            raise UploadRejected(
                f"marking a document confidential requires the {department}:confidential permission"
            )
        return "confidential"
    if requested == "public":
        if not principal.has(PUBLISH_PERMISSION):
            raise UploadRejected(
                "publishing a company-wide (public) document requires a publish permission"
            )
        return "public"
    raise UploadRejected(f"unknown classification {requested!r}")


def safe_suffix(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise UploadRejected(f"unsupported file type {suffix or '(none)'}")
    return suffix


def scan_for_secrets(text: str) -> None:
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            raise UploadRejected(
                "the file appears to contain a credential; remove it before uploading"
            )


FRONT_MATTER = re.compile(r"^---\n(.*?)\n---\n", re.S)


def claimed_metadata(text: str) -> dict[str, str]:
    """What the file says about itself - information, not instruction."""
    match = FRONT_MATTER.match(text)
    if not match:
        return {}
    claims = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            claims[key.strip()] = value.strip().strip('"')
    return claims


def check_claims(claims: dict[str, str], department: str, classification: str) -> None:
    if (claimed := claims.get("department")) and claimed != department:
        raise UploadRejected(
            f"the file declares department {claimed!r} but you may only write to {department!r}"
        )
    if (claimed := claims.get("classification")) and claimed != classification:
        raise UploadRejected(
            f"the file declares classification {claimed!r}; it is being stored as "
            f"{classification!r}. Remove the field or request that classification explicitly."
        )


async def next_doc_key(session: AsyncSession, department: str) -> str:
    prefix = f"KB-{department[:3].upper()}"
    used = (
        await session.scalars(select(Document.doc_key).where(Document.doc_key.like(f"{prefix}-%")))
    ).all()
    numbers = [int(k.rsplit("-", 1)[1]) for k in set(used) if k.rsplit("-", 1)[1].isdigit()]
    return f"{prefix}-{max(numbers, default=100) + 1:03d}"


def render_markdown(plan: UploadPlan, body: str, principal: Principal) -> str:
    """Server-authored front matter replaces anything the file claimed."""
    body = FRONT_MATTER.sub("", body).lstrip()
    today = datetime.now(UTC).date().isoformat()
    return (
        "---\n"
        f"document_id: {plan.doc_key}\n"
        f"title: {plan.title}\n"
        f"department: {plan.department}\n"
        f"classification: {plan.classification}\n"
        f"updated_at: {today}\n"
        f"source: uploaded by {principal.user_id}\n"
        "---\n\n" + body + "\n"
    )


def sidecar_metadata(plan: UploadPlan, principal: Principal) -> dict[str, str]:
    return {
        "document_id": plan.doc_key,
        "title": plan.title,
        "department": plan.department,
        "classification": plan.classification,
        "updated_at": datetime.now(UTC).date().isoformat(),
        "source": f"uploaded by {principal.user_id}",
    }


def upload_kind(suffix: str) -> Literal["markdown", "sidecar"]:
    return "markdown" if suffix in (".md", ".markdown") else "sidecar"


async def touch_provenance(session: AsyncSession, doc_key: str, user_id: str) -> None:
    """Record who contributed the active version (shown with citations)."""
    await session.execute(
        update(Document)
        .where(Document.doc_key == doc_key, Document.status == "active")
        .values(uploaded_by=user_id)
    )


def upload_root(knowledge_root: Path) -> Path:
    root = knowledge_root / UPLOAD_SUBDIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def new_upload_id() -> str:
    return uuid.uuid4().hex[:12]


async def document_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(Document)) or 0)
