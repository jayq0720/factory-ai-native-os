"""文档上传与解析结果（方案 4.7：/api/eng/projects/{id}/documents 等会建立在这些接口之上）。

W4 先同步解析：上传即解析、返回结果。Worker 运行时（W7–W8）就位后改为异步任务。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile

from fos.api.deps import SessionDep, require
from fos.db.models import Document, DocumentPage
from fos.governance.principal import Principal
from fos.perception.service import IngestError, ingest_document, load_pages

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


@router.post("", status_code=201)
async def upload(
    request: Request,
    session: SessionDep,
    principal: Writer,
    file: UploadFile,
    kind: Annotated[DocKind, Form()] = "other",
) -> dict[str, Any]:
    st = request.app.state
    data = await file.read(st.settings.max_upload_mb * 1024 * 1024 + 1)
    try:
        r = await ingest_document(
            session,
            tenant_id=principal.tenant_id,
            uploaded_by=principal.subject_id,
            data=data,
            filename=file.filename or "upload",
            kind=kind,
            ocr=st.ocr,
            blobs=st.blobs,
            settings=st.settings,
        )
    except IngestError as e:
        raise HTTPException(e.code, detail=str(e)) from e
    return {**_doc_out(r.document, r.pages), "duplicate": r.duplicate}


async def _get_doc(session, principal: Principal, doc_id: str) -> Document:
    d = await session.get(Document, doc_id)
    if d is None or d.tenant_id != principal.tenant_id:
        raise HTTPException(404, detail="文档不存在")
    return d


@router.get("/{doc_id}")
async def get_document(doc_id: str, session: SessionDep, principal: Reader) -> dict[str, Any]:
    d = await _get_doc(session, principal, doc_id)
    return _doc_out(d, await load_pages(session, d.id))


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
