"""文档入库：存原件 → 去重 → 解析 → 写文档与逐页结果 → 字段抽取。

两种用法共用这里的步骤，保证去重、限额和 data_mode 规则一致：
- ingest_document()：同步，解析完再返回（IM 收图、`wait=true` 的上传）；
- register_document() + DocumentJobs：先登记、立即返回，后台解析并更新进度（网页上传大文件）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.config import Settings
from fos.db.models import Document, DocumentPage
from fos.perception.blobs import LocalBlobStore
from fos.perception.ocr import OCRError, OCRProvider
from fos.perception.parser import ParseError, ProgressFn, parse, sniff
from fos.perception.types import ParsedDocument

if TYPE_CHECKING:
    from fos.extraction.service import Extractor


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


async def register_document(
    session: AsyncSession,
    *,
    tenant_id: str,
    uploaded_by: str,
    data: bytes,
    filename: str,
    kind: str,
    blobs: LocalBlobStore,
    settings: Settings,
) -> tuple[Document, bool]:
    """存原件并登记（status=queued）。返回 (文档, 是否重复)。之前解析失败的同一文件会重新排队。"""
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
    if existing is not None:
        if existing.status != "failed":  # 同一文件不重复解析
            return existing, True
        await session.execute(delete(DocumentPage).where(DocumentPage.document_id == existing.id))
        existing.status, existing.error, existing.progress = "queued", None, 0
        await session.flush()
        return existing, False

    doc = Document(
        tenant_id=tenant_id,
        sha256=digest,
        filename=(filename or "upload")[:255],
        format=fmt,
        size_bytes=len(data),
        blob_uri=blobs.uri(tenant_id, digest),
        kind=kind,
        uploaded_by=uploaded_by,
        status="queued",
        data_mode="real",
        page_count=0,
        progress=0,
    )
    session.add(doc)
    await session.flush()
    return doc, False


async def parse_bytes(
    data: bytes, ocr: OCRProvider, settings: Settings, on_progress: ProgressFn | None = None
) -> ParsedDocument | str:
    """解析原件。成功返回结果；OCR 服务出错返回错误说明（文档记为 failed，可重试）。"""
    try:
        return await parse(
            data,
            ocr,
            max_pages=settings.max_pages,
            render_scale=settings.ocr_render_scale,
            concurrency=settings.ocr_concurrency,
            on_progress=on_progress,
        )
    except OCRError as e:
        return str(e)


async def store_parsed(
    session: AsyncSession, doc: Document, parsed: ParsedDocument | str, ocr: OCRProvider
) -> list[DocumentPage]:
    await session.execute(delete(DocumentPage).where(DocumentPage.document_id == doc.id))
    if isinstance(parsed, str):
        doc.status, doc.error, doc.data_mode = "failed", parsed[:500], ocr.data_mode
        await session.flush()
        return []
    doc.data_mode = parsed.data_mode
    doc.page_count = doc.progress = len(parsed.pages)
    doc.status, doc.error = "parsed", None
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
    return await load_pages(session, doc.id)


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
    extractor: Extractor | None = None,
) -> IngestResult:
    doc, duplicate = await register_document(
        session,
        tenant_id=tenant_id,
        uploaded_by=uploaded_by,
        data=data,
        filename=filename,
        kind=kind,
        blobs=blobs,
        settings=settings,
    )
    if duplicate:
        return IngestResult(doc, await load_pages(session, doc.id), duplicate=True)
    try:
        parsed = await parse_bytes(data, ocr, settings)
    except ParseError as e:
        doc.status, doc.error = "failed", str(e)[:500]
        await session.flush()
        raise IngestError(str(e), 422) from e
    pages = await store_parsed(session, doc, parsed, ocr)
    if extractor is not None and pages:
        await extractor.run(session, doc, pages)
    return IngestResult(doc, pages, duplicate=False)
