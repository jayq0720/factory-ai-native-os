"""文档入库：存原件 → 去重 → 解析 → 写文档与逐页结果。

上传接口和 IM 收图共用这一个入口，保证两条路径的去重、限额和 data_mode 规则一致。
W7–W8 Worker 运行时就位后，这里改为提交异步任务。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.config import Settings
from fos.db.models import Document, DocumentPage
from fos.perception.blobs import LocalBlobStore
from fos.perception.ocr import OCRError, OCRProvider
from fos.perception.parser import ParseError, parse, sniff


class IngestError(ValueError):
    """输入本身有问题（过大、为空、格式不支持、解析失败）。code 对应 HTTP 状态码。"""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


@dataclass
class IngestResult:
    document: Document
    pages: list[DocumentPage]
    duplicate: bool


async def load_pages(session: AsyncSession, doc_id: str) -> list[DocumentPage]:
    return list(
        (
            await session.scalars(
                select(DocumentPage).where(DocumentPage.document_id == doc_id).order_by(DocumentPage.page)
            )
        ).all()
    )


def check_size(data: bytes, settings: Settings) -> None:
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise IngestError(f"文件超过 {settings.max_upload_mb} MB", 413)
    if not data:
        raise IngestError("文件为空", 422)


async def ingest_document(
    session: AsyncSession,
    *,
    tenant_id: str,
    uploaded_by: str,
    data: bytes,
    filename: str,
    kind: str,
    ocr: OCRProvider,
    blobs: LocalBlobStore,
    settings: Settings,
) -> IngestResult:
    check_size(data, settings)
    try:
        fmt = sniff(data)
    except ParseError as e:
        raise IngestError(str(e), 415) from e

    digest = blobs.put(tenant_id, data)
    existing = (
        await session.scalars(
            select(Document).where(Document.tenant_id == tenant_id, Document.sha256 == digest)
        )
    ).one_or_none()
    if existing is not None:  # 同一文件不重复解析
        return IngestResult(existing, await load_pages(session, existing.id), duplicate=True)

    doc = Document(
        tenant_id=tenant_id,
        sha256=digest,
        filename=(filename or "upload")[:255],
        format=fmt,
        size_bytes=len(data),
        blob_uri=blobs.uri(tenant_id, digest),
        kind=kind,
        uploaded_by=uploaded_by,
        status="parsed",
        data_mode="real",
    )
    try:
        parsed = await parse(data, ocr, max_pages=settings.max_pages, render_scale=settings.ocr_render_scale)
    except ParseError as e:
        raise IngestError(str(e), 422) from e
    except OCRError as e:
        doc.status, doc.error, doc.data_mode = "failed", str(e)[:500], ocr.data_mode
        session.add(doc)
        await session.flush()
        return IngestResult(doc, [], duplicate=False)

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
    return IngestResult(doc, await load_pages(session, doc.id), duplicate=False)
