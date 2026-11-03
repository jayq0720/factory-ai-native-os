"""文档上传、解析结果、字段核对与导出（方案 4.2 第 1–4 步、4.7）。

- 上传：`wait=true`（默认）解析完再返回；网页端用 `wait=false`，立即返回 202，后台解析，轮询进度。
- 字段：每个字段带页码与原文片段；人工修改、确认都记在字段的 history 里。
- 导出：Excel / JSON。模拟数据（data_mode=mock）不能导出正式文件。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from fos.api.deps import SessionDep, require
from fos.db.models import Document, DocumentField, DocumentPage
from fos.extraction.export import field_out, summary, to_json, to_xlsx
from fos.extraction.service import HUMAN_STATUSES, load_fields
from fos.governance.principal import Principal
from fos.perception.parser import render_page_png
from fos.perception.service import IngestError, ingest_document, load_pages, register_document

router = APIRouter(prefix="/v1")

DocKind = Literal["tech", "tender", "drawing", "boq", "receipt", "report", "other"]
Writer = Annotated[Principal, Depends(require("doc.document:write"))]
Reader = Annotated[Principal, Depends(require("doc.document:read"))]
Editor = Annotated[Principal, Depends(require("doc.field:write"))]
MOCK_NOTICE = "模拟数据，非真实识别结果"
MIME = {
    "pdf": "application/pdf",
    "image": "application/octet-stream",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


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
        "progress": d.progress,
        "schema_id": d.schema_id,
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
        out["notice"] = MOCK_NOTICE
    return out


@router.get("/documents")
async def list_documents(
    session: SessionDep,
    principal: Reader,
    kind: DocKind | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    q = select(Document).where(Document.tenant_id == principal.tenant_id)
    if kind:
        q = q.where(Document.kind == kind)
    total = await session.scalar(select(func.count()).select_from(q.subquery()))
    docs = list(
        (await session.scalars(q.order_by(Document.created_at.desc()).limit(limit).offset(offset))).all()
    )
    # 每份文档的缺失必填 / 冲突数，列表页直接显示
    stats: dict[str, dict[str, int]] = {}
    if docs:
        rows = await session.execute(
            select(
                DocumentField.document_id,
                DocumentField.status,
                DocumentField.required,
                DocumentField.conflict,
            )
            .where(DocumentField.tenant_id == principal.tenant_id)
            .where(DocumentField.document_id.in_([d.id for d in docs]))
        )
        for doc_id, status, required, conflict in rows:
            s = stats.setdefault(doc_id, {"fields": 0, "missing_required": 0, "conflicts": 0, "confirmed": 0})
            s["fields"] += 1
            s["missing_required"] += int(status == "missing" and bool(required))
            s["conflicts"] += int(bool(conflict))
            s["confirmed"] += int(status == "confirmed")
    return {
        "total": total,
        "items": [{**_doc_out(d), "fields": stats.get(d.id)} for d in docs],
    }


@router.post("/documents", status_code=201)
async def upload(
    request: Request,
    session: SessionDep,
    principal: Writer,
    file: UploadFile,
    kind: Annotated[DocKind, Form()] = "other",
    wait: bool = True,
):
    st = request.app.state
    data = await file.read(st.settings.max_upload_mb * 1024 * 1024 + 1)
    try:
        if wait:
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
                extractor=st.extractor,
            )
            return {**_doc_out(r.document, r.pages), "duplicate": r.duplicate}
        doc, duplicate = await register_document(
            session,
            tenant_id=principal.tenant_id,
            uploaded_by=principal.subject_id,
            data=data,
            filename=file.filename or "upload",
            kind=kind,
            blobs=st.blobs,
            settings=st.settings,
        )
    except IngestError as e:
        raise HTTPException(e.code, detail=str(e)) from e
    out = {**_doc_out(doc), "duplicate": duplicate}
    await session.commit()  # 先提交，后台任务才读得到
    if not duplicate:
        st.jobs.submit(doc.id)
    return JSONResponse(out, status_code=200 if duplicate else 202)


async def _get_doc(session, principal: Principal, doc_id: str) -> Document:
    d = await session.get(Document, doc_id)
    if d is None or d.tenant_id != principal.tenant_id:
        raise HTTPException(404, detail="文档不存在")
    return d


@router.get("/documents/{doc_id}")
async def get_document(
    doc_id: str, request: Request, session: SessionDep, principal: Reader
) -> dict[str, Any]:
    d = await _get_doc(session, principal, doc_id)
    out = _doc_out(d, await load_pages(session, d.id))
    out["fields_summary"] = summary(await load_fields(session, d.id))
    return out


@router.post("/documents/{doc_id}/retry", status_code=202)
async def retry(doc_id: str, request: Request, session: SessionDep, principal: Writer) -> dict[str, Any]:
    """解析失败（如 OCR 服务暂时不可用）的文档重新排队。"""
    d = await _get_doc(session, principal, doc_id)
    if d.status != "failed":
        raise HTTPException(409, detail="只有解析失败的文档可以重试")
    d.status, d.error, d.progress = "queued", None, 0
    out = _doc_out(d)
    await session.commit()
    request.app.state.jobs.submit(d.id)
    return out


@router.get("/documents/{doc_id}/pages/{page}")
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
        **({"notice": MOCK_NOTICE} if p.data_mode == "mock" else {}),
    }


@router.get("/documents/{doc_id}/pages/{page}/image")
async def page_image(
    doc_id: str,
    page: int,
    request: Request,
    session: SessionDep,
    principal: Reader,
    scale: Annotated[float, Query(ge=0.5, le=3.0)] = 1.5,
) -> Response:
    """原件页面预览图（界面左侧原文对照）。Word / Excel 没有版面，返回 404。"""
    d = await _get_doc(session, principal, doc_id)
    data = request.app.state.blobs.get(d.tenant_id, d.sha256)
    png = render_page_png(data, page, scale=scale)
    if png is None:
        raise HTTPException(404, detail="该页没有可预览的版面")
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/documents/{doc_id}/original")
async def original(doc_id: str, request: Request, session: SessionDep, principal: Reader) -> Response:
    d = await _get_doc(session, principal, doc_id)
    data = request.app.state.blobs.get(d.tenant_id, d.sha256)
    return Response(
        data,
        media_type=MIME.get(d.format, "application/octet-stream"),
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(d.filename)}"},
    )


# ---------------- 字段 ----------------


@router.get("/documents/{doc_id}/fields")
async def get_fields(doc_id: str, request: Request, session: SessionDep, principal: Reader) -> dict[str, Any]:
    d = await _get_doc(session, principal, doc_id)
    fields = await load_fields(session, d.id)
    sd = request.app.state.extractor.schema_for(d.tenant_id, d.kind, d.schema_id)
    order = {f.key: i for i, f in enumerate(sd.fields)} if sd else {}
    fields.sort(key=lambda f: order.get(f.key, 999))
    schema = {"id": sd.id, "name": sd.name, "version": sd.version, "groups": sd.groups} if sd else None
    out: dict[str, Any] = {
        "document_id": d.id,
        "status": d.status,
        "data_mode": d.data_mode,
        "schema": schema,
        "llm_extraction": request.app.state.extractor.uses_llm,
        "summary": summary(fields),
        "fields": [field_out(f) for f in fields],
    }
    if d.data_mode == "mock":
        out["notice"] = MOCK_NOTICE
    return out


class FieldPatch(BaseModel):
    action: Literal["correct", "confirm", "reset"] = "correct"
    value: str | None = Field(default=None, max_length=2000)
    unit: str | None = Field(default=None, max_length=16)
    note: str | None = Field(default=None, max_length=200)


@router.patch("/documents/{doc_id}/fields/{key}")
async def patch_field(
    doc_id: str, key: str, body: FieldPatch, session: SessionDep, principal: Editor
) -> dict[str, Any]:
    d = await _get_doc(session, principal, doc_id)
    f = await session.get(DocumentField, (d.id, key))
    if f is None:
        raise HTTPException(404, detail=f"字段 {key} 不存在")
    entry: dict[str, Any] = {
        "at": datetime.now(UTC).isoformat(),
        "by": principal.subject_id,
        "by_name": principal.display_name,
        "action": body.action,
        "before": {"value": f.value, "unit": f.unit, "status": f.status},
    }
    if body.action == "correct":
        value = (body.value or "").strip() or None
        if value is None:
            raise HTTPException(422, detail="请填写修改后的值")
        f.value, f.unit, f.status, f.method = value, (body.unit or "").strip() or None, "corrected", "manual"
    elif body.action == "confirm":
        if not f.value:
            raise HTTPException(422, detail="缺失的字段请先填写值")
        f.status = "confirmed"
    else:  # reset：回到抽取结果的第一候选
        best = (f.candidates or [None])[0]
        if best:
            f.value, f.unit, f.method = best["value"], best.get("unit"), best.get("method", "rule")
            f.confidence, f.sources, f.status = (
                best.get("confidence", 0.0),
                best.get("sources", []),
                "extracted",
            )
        else:
            f.value, f.status, f.method, f.sources, f.confidence = None, "missing", "none", [], 0.0
    if body.note:
        entry["note"] = body.note
    entry["after"] = {"value": f.value, "unit": f.unit, "status": f.status}
    f.history = [*(f.history or []), entry]
    f.updated_at = datetime.now(UTC)
    await session.flush()
    return field_out(f)


class ExtractIn(BaseModel):
    schema_id: str | None = None


@router.post("/documents/{doc_id}/extract")
async def re_extract(
    doc_id: str, request: Request, session: SessionDep, principal: Editor, body: ExtractIn | None = None
) -> dict[str, Any]:
    """重新抽取（改了 Schema 叫法、或换一份 Schema）。人工修改 / 确认过的字段保持不变。"""
    d = await _get_doc(session, principal, doc_id)
    if d.status != "parsed":
        raise HTTPException(409, detail="文档还没有解析完成")
    ex = request.app.state.extractor
    if body and body.schema_id:
        if ex.schema_for(d.tenant_id, d.kind, body.schema_id) is None:
            raise HTTPException(422, detail=f"没有 Schema {body.schema_id}")
        if body.schema_id != d.schema_id:
            for old in await load_fields(session, d.id):
                await session.delete(old)
            await session.flush()
            d.schema_id = body.schema_id
    fields = await ex.run(session, d, await load_pages(session, d.id))
    if fields is None:
        raise HTTPException(422, detail=f"文件类别 {d.kind} 没有对应的抽取 Schema，请选择一份")
    kept = sum(f.status in HUMAN_STATUSES for f in fields)
    return {
        "document_id": d.id,
        "schema_id": d.schema_id,
        "summary": summary(fields),
        "kept_human_edits": kept,
    }


@router.get("/documents/{doc_id}/export")
async def export(
    doc_id: str,
    request: Request,
    session: SessionDep,
    principal: Reader,
    format: Literal["xlsx", "json"] = "xlsx",
) -> Response:
    d = await _get_doc(session, principal, doc_id)
    if d.status != "parsed":
        raise HTTPException(409, detail="文档还没有解析完成")
    pages = await load_pages(session, d.id)
    fields = await load_fields(session, d.id)
    schema = request.app.state.extractor.schema_for(d.tenant_id, d.kind, d.schema_id)
    if schema:
        order = {f.key: i for i, f in enumerate(schema.fields)}
        fields.sort(key=lambda f: order.get(f.key, 999))
    if format == "json":
        return JSONResponse(to_json(d, pages, fields))
    if d.data_mode == "mock":
        raise HTTPException(409, detail="模拟数据不能导出正式文件；接入真实 OCR 后重新上传")
    data = to_xlsx(
        d,
        pages,
        fields,
        exported_by=principal.display_name,
        schema_name=schema.name if schema else "",
        group_order=schema.groups if schema else None,
    )
    name = d.filename.rsplit(".", 1)[0] + "-字段.xlsx"
    return Response(
        data,
        media_type=MIME["xlsx"],
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


@router.get("/extraction/schemas")
async def list_schemas(request: Request, principal: Reader) -> dict[str, Any]:
    ts = request.app.state.extractor.schemas.get(principal.tenant_id, {})
    return {
        "items": [
            {
                "id": s.id,
                "name": s.name,
                "version": s.version,
                "kinds": s.kinds,
                "groups": s.groups,
                "fields": [
                    {"key": f.key, "name": f.name, "group": f.group, "type": f.type, "required": f.required}
                    for f in s.fields
                ],
            }
            for s in ts.values()
        ]
    }
