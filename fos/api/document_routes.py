"""文档上传与解析结果（方案 4.7：/api/eng/projects/{id}/documents 等会建立在这些接口之上）。

W4 先同步解析：上传即解析、返回结果。Worker 运行时（W7–W8）就位后改为异步任务。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from sqlalchemy import select

from fos.api.deps import SessionDep, require
from fos.db.models import Document, DocumentPage
from fos.governance.principal import Principal
from fos.perception.ocr import OCRError
from fos.perception.parser import ParseError, parse, sniff

router = APIRouter(prefix="/v1/documents")

DocKind = Literal["tech", "tender", "drawing", "boq", "receipt", "report", "other"]
Writer = Annotated[Principal, Depends(require("doc.document:write"))]
Reader = Annotated[Principal, Depends(require("doc.document:read"))]


def _doc_out(d: Document, pages: list[DocumentPage] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": d.id,
        "filename": d.filename,
        "format": d.format,
        "kind": d.kind,
        "size_bytes": d.size_bytes,
        "sha256": d.sha256,
        "status": d.status,
        "error": d.error,
        "data_mode": d.data_mode,
        "page_count": d.page_count,
        "uploaded_by": d.uploaded_by,
        "created_at": d.created_at.isoformat(),
    }
    if pages is not None:
        out["pages"] = [
            {
                "page": p.page,
                "method": p.method,
                "data_mode": p.data_mode,
                "title": p.title,
                "chars": len(p.markdown),
            }
            for p in pages
        ]
    if d.data_mode == "mock":
        out["notice"] = "模拟数据，非真实识别结果"
    return out


async def _pages(session, doc_id: str) -> list[DocumentPage]:
    return list(
        (
            await session.scalars(
                select(DocumentPage).where(DocumentPage.document_id == doc_id).order_by(DocumentPage.page)
            )
        ).all()
    )


@router.post("", status_code=201)
async def upload(
    request: Request,
    session: SessionDep,
    principal: Writer,
    file: UploadFile,
    kind: Annotated[DocKind, Form()] = "other",
) -> dict[str, Any]:
    s = request.app.state.settings
    limit = s.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, detail=f"文件超过 {s.max_upload_mb} MB")
    if not data:
        raise HTTPException(422, detail="文件为空")
    try:
        fmt = sniff(data)
    except ParseError as e:
        raise HTTPException(415, detail=str(e)) from e

    blobs = request.app.state.blobs
    digest = blobs.put(principal.tenant_id, data)
    existing = (
        await session.scalars(
            select(Document).where(Document.tenant_id == principal.tenant_id, Document.sha256 == digest)
        )
    ).one_or_none()
    if existing is not None:  # 同一文件不重复解析
        return {**_doc_out(existing, await _pages(session, existing.id)), "duplicate": True}

    doc = Document(
        tenant_id=principal.tenant_id,
        sha256=digest,
        filename=(file.filename or "upload")[:255],
        format=fmt,
        size_bytes=len(data),
        blob_uri=blobs.uri(principal.tenant_id, digest),
        kind=kind,
        uploaded_by=principal.subject_id,
        status="parsed",
        data_mode="real",
    )
    try:
        parsed = await parse(
            data, request.app.state.ocr, max_pages=s.max_pages, render_scale=s.ocr_render_scale
        )
    except ParseError as e:
        raise HTTPException(422, detail=str(e)) from e
    except OCRError as e:
        doc.status, doc.error, doc.data_mode = "failed", str(e)[:500], request.app.state.ocr.data_mode
        session.add(doc)
        await session.flush()
        return {**_doc_out(doc, []), "duplicate": False}

    doc.data_mode = parsed.data_mode
    doc.page_count = len(parsed.pages)
    session.add(doc)
    await session.flush()
    for p in parsed.pages:
        session.add(
            DocumentPage(
                document_id=doc.id,
                page=p.page,
                method=p.method,
                data_mode=p.data_mode,
                title=p.title,
                markdown=p.markdown,
            )
        )
    await session.flush()
    return {**_doc_out(doc, await _pages(session, doc.id)), "duplicate": False}


async def _get_doc(session, principal: Principal, doc_id: str) -> Document:
    d = await session.get(Document, doc_id)
    if d is None or d.tenant_id != principal.tenant_id:
        raise HTTPException(404, detail="文档不存在")
    return d


@router.get("/{doc_id}")
async def get_document(doc_id: str, session: SessionDep, principal: Reader) -> dict[str, Any]:
    d = await _get_doc(session, principal, doc_id)
    return _doc_out(d, await _pages(session, d.id))


@router.get("/{doc_id}/pages/{page}")
async def get_page(doc_id: str, page: int, session: SessionDep, principal: Reader) -> dict[str, Any]:
    d = await _get_doc(session, principal, doc_id)
    p = await session.get(DocumentPage, (d.id, page))
    if p is None:
        raise HTTPException(404, detail=f"第 {page} 页不存在")
    return {
        "document_id": d.id,
        "page": p.page,
        "page_count": d.page_count,
        "method": p.method,
        "data_mode": p.data_mode,
        "title": p.title,
        "markdown": p.markdown,
        **({"notice": "模拟数据，非真实识别结果"} if p.data_mode == "mock" else {}),
    }
