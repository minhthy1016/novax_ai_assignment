"""Document upload by authorized users (see knowledge/upload.py for the threat model)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Request, UploadFile

from opsassist.api.common import error_response, request_id_of
from opsassist.api.schemas import ErrorResponse, UploadResponse
from opsassist.auth import CurrentPrincipal
from opsassist.knowledge import upload as up
from opsassist.logging_setup import get_logger
from opsassist.policy import audit
from opsassist.worker import enqueue_ingestion

router = APIRouter(prefix="/api/documents", tags=["knowledge"])
log = get_logger("opsassist.upload")


@router.post(
    "",
    response_model=UploadResponse,
    responses={
        400: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        413: {"model": ErrorResponse},
    },
)
async def upload_document(
    request: Request,
    principal: CurrentPrincipal,
    file: Annotated[UploadFile, File(description="Markdown, plain text or PDF")],
    title: Annotated[str | None, Form(max_length=200)] = None,
    classification: Annotated[str | None, Form()] = None,
) -> Any:
    """Store a document in the uploader's own department and queue it for indexing.

    The department comes from the caller's `kb:write:<department>` permission; metadata
    inside the file cannot widen or change it.
    """
    request_id = request_id_of(request)
    settings = request.app.state.settings
    factory = request.app.state.session_factory

    async def deny(status: int, code: str, message: str) -> Any:
        async with factory() as session, session.begin():
            await audit.append(
                session,
                audit.entry_of(
                    principal,
                    request_id=request_id,
                    event="upload",
                    decision="deny",
                    reason=message,
                    arguments={"filename": file.filename, "classification": classification},
                ),
            )
        return error_response(status, code, message, request_id)

    departments = up.writable_departments(principal)
    if not departments:
        return await deny(403, "forbidden", "you have no kb:write permission for any department")
    if len(departments) > 1:
        return await deny(400, "ambiguous_department", "your permissions span several departments")
    department = departments.pop()

    try:
        suffix = up.safe_suffix(file.filename or "")
        chosen = up.choose_classification(principal, department, classification)
    except up.UploadRejected as exc:
        return await deny(403 if classification else 400, "upload_rejected", str(exc))

    raw = await file.read(up.MAX_UPLOAD_BYTES + 1)
    if len(raw) > up.MAX_UPLOAD_BYTES:
        return await deny(413, "too_large", f"files are limited to {up.MAX_UPLOAD_BYTES} bytes")
    if not raw:
        return await deny(400, "empty_file", "the uploaded file is empty")

    text = raw.decode("utf-8", errors="ignore") if suffix != ".pdf" else ""
    try:
        if text:
            up.scan_for_secrets(text)
            up.check_claims(up.claimed_metadata(text), department, chosen)
    except up.UploadRejected as exc:
        return await deny(400, "upload_rejected", str(exc))

    async with factory() as session:
        doc_key = await up.next_doc_key(session, department)
    plan = up.UploadPlan(
        doc_key=doc_key,
        department=department,
        classification=chosen,
        title=(title or Path(file.filename or doc_key).stem)[:200],
        path=up.upload_root(settings.knowledge_root) / f"{doc_key}{suffix}",
    )

    if up.upload_kind(suffix) == "markdown":
        plan.path.write_text(up.render_markdown(plan, text, principal), encoding="utf-8")
    else:
        plan.path.write_bytes(raw)
        plan.path.with_name(plan.path.name + ".meta.json").write_text(
            json.dumps(up.sidecar_metadata(plan, principal), indent=1), encoding="utf-8"
        )

    stored_as = str(plan.path.relative_to(settings.knowledge_root.parent.parent))
    job_id = await enqueue_ingestion(plan.path)
    async with factory() as session, session.begin():
        await audit.append(
            session,
            audit.entry_of(
                principal,
                request_id=request_id,
                event="upload",
                decision="allow",
                reason=f"queued for indexing as {doc_key}",
                arguments={
                    "filename": file.filename,
                    "department": department,
                    "classification": chosen,
                    "bytes": len(raw),
                },
                result={"doc_key": doc_key, "job_id": job_id},
            ),
        )
    log.info(
        "document_uploaded",
        doc_key=doc_key,
        department=department,
        classification=chosen,
        user=principal.user_id,
    )
    return UploadResponse(
        doc_key=doc_key,
        department=department,
        classification=chosen,
        title=plan.title,
        job_id=job_id,
        stored_as=stored_as,
        request_id=request_id,
    )
