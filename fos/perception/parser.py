"""文档解析管线（方案 4.2 第 2 步）。

- PDF：逐页判断。有文字层直接取字（快、准）；没有文字层的扫描页渲染成 PNG 交给 OCR。
- 图片（PNG / JPEG / TIFF）：转 PNG 后交给 OCR；多帧 TIFF 每帧一页。
- Word（.docx）：按正文顺序输出段落与表格，作为一页。
- Excel（.xlsx）：每个工作表一页，输出 HTML 表格（与 OCR 的表格格式一致）。

每页记录解析方式和 data_mode，模拟 OCR 的页会让整份文档变成 mock。
"""

from __future__ import annotations

import asyncio
import html
import io
import zipfile
from collections.abc import Awaitable, Callable

import pypdfium2 as pdfium
from PIL import Image, UnidentifiedImageError

from fos.perception.ocr import OCRProvider
from fos.perception.types import DocFormat, PageResult, ParsedDocument

TEXT_LAYER_MIN_CHARS = 30  # 少于这么多非空白字符就当扫描页处理
MAX_XLSX_ROWS = 2000


class ParseError(ValueError):
    pass


def sniff(data: bytes) -> DocFormat:
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith((b"\x89PNG", b"\xff\xd8\xff", b"II*\x00", b"MM\x00*")):
        return "image"
    if data.startswith(b"PK\x03\x04"):
        try:
            names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
        except zipfile.BadZipFile as e:
            raise ParseError("压缩包已损坏") from e
        if "word/document.xml" in names:
            return "docx"
        if "xl/workbook.xml" in names:
            return "xlsx"
    raise ParseError("不支持的文件格式（支持 PDF、PNG、JPEG、TIFF、DOCX、XLSX）")


ProgressFn = Callable[[int, int], Awaitable[None]]


async def parse(
    data: bytes,
    ocr: OCRProvider,
    *,
    max_pages: int = 300,
    render_scale: float = 2.0,
    concurrency: int = 4,
    on_progress: ProgressFn | None = None,
) -> ParsedDocument:
    """on_progress(已完成页数, 总页数)：每完成一页回调一次，后台任务用来更新进度。"""
    fmt = sniff(data)
    if fmt == "pdf":
        pages = await _parse_pdf(data, ocr, max_pages, render_scale, concurrency, on_progress)
    elif fmt == "image":
        pages = await _parse_image(data, ocr, max_pages, concurrency, on_progress)
    elif fmt == "docx":
        pages = [_parse_docx(data)]
    else:
        pages = _parse_xlsx(data, max_pages)
    return ParsedDocument(format=fmt, pages=pages)


def count_pages(data: bytes) -> int:
    """不解析，只数页数（后台任务显示进度用）。docx 记 1 页，xlsx 按工作表。"""
    fmt = sniff(data)
    try:
        if fmt == "pdf":
            pdf = pdfium.PdfDocument(data)
            try:
                return len(pdf)
            finally:
                pdf.close()
        if fmt == "image":
            return getattr(Image.open(io.BytesIO(data)), "n_frames", 1)
        if fmt == "xlsx":
            from openpyxl import load_workbook

            wb = load_workbook(io.BytesIO(data), read_only=True)
            try:
                return len(wb.sheetnames)
            finally:
                wb.close()
    except Exception:  # 数不出来就交给 parse 报具体错误
        return 0
    return 1


def _ocr_method(ocr: OCRProvider) -> str:
    return "ocr_mock" if ocr.data_mode == "mock" else "ocr"


async def _run_all(jobs: list[Callable[[], Awaitable[None]]], concurrency: int) -> None:
    """并发执行，任何一页失败就取消其余页并抛出第一个错误。"""
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(job: Callable[[], Awaitable[None]]) -> None:
        async with sem:
            await job()

    try:
        async with asyncio.TaskGroup() as tg:
            for j in jobs:
                tg.create_task(one(j))
    except ExceptionGroup as eg:
        raise eg.exceptions[0] from None


async def _parse_pdf(
    data: bytes,
    ocr: OCRProvider,
    max_pages: int,
    scale: float,
    concurrency: int,
    on_progress: ProgressFn | None,
) -> list[PageResult]:
    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as e:
        raise ParseError(f"PDF 无法打开：{e}") from e
    try:
        n = len(pdf)
        if n > max_pages:
            raise ParseError(f"PDF 共 {n} 页，超过上限 {max_pages} 页")
        out: list[PageResult | None] = [None] * n
        scanned: list[int] = []
        for i in range(n):
            page = pdf[i]
            try:
                tp = page.get_textpage()
                text = tp.get_text_range().replace("\r\n", "\n").replace("\r", "\n")
                tp.close()
            finally:
                page.close()
            if sum(not c.isspace() for c in text) >= TEXT_LAYER_MIN_CHARS:
                out[i] = PageResult(
                    page=i + 1, method="text_layer", markdown=text.strip(), data_mode="real", chars=len(text)
                )
            else:
                scanned.append(i)
        done = n - len(scanned)
        if on_progress:
            await on_progress(done, n)

        def job(i: int) -> Callable[[], Awaitable[None]]:
            async def run() -> None:
                nonlocal done
                page = pdf[i]  # 渲染是同步调用，在事件循环里串行执行，pdfium 不会被并发访问
                try:
                    png = _png(page.render(scale=scale).to_pil())
                finally:
                    page.close()
                md = await ocr.recognize(png, page=i + 1)
                out[i] = PageResult(
                    page=i + 1, method=_ocr_method(ocr), markdown=md, data_mode=ocr.data_mode, chars=len(md)
                )
                done += 1
                if on_progress:
                    await on_progress(done, n)

            return run

        await _run_all([job(i) for i in scanned], concurrency)
        return [p for p in out if p is not None]
    finally:
        pdf.close()


async def _parse_image(
    data: bytes, ocr: OCRProvider, max_pages: int, concurrency: int, on_progress: ProgressFn | None
) -> list[PageResult]:
    try:
        img = Image.open(io.BytesIO(data))
    except (UnidentifiedImageError, Image.DecompressionBombError) as e:
        raise ParseError(f"图片无法读取：{e}") from e
    frames = getattr(img, "n_frames", 1)
    if frames > max_pages:
        raise ParseError(f"图片共 {frames} 帧，超过上限 {max_pages}")
    pngs: list[bytes] = []
    for i in range(frames):
        img.seek(i)
        pngs.append(_png(img))
    out: list[PageResult | None] = [None] * frames
    done = 0

    def job(i: int) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            nonlocal done
            md = await ocr.recognize(pngs[i], page=i + 1)
            out[i] = PageResult(
                page=i + 1, method=_ocr_method(ocr), markdown=md, data_mode=ocr.data_mode, chars=len(md)
            )
            done += 1
            if on_progress:
                await on_progress(done, frames)

        return run

    await _run_all([job(i) for i in range(frames)], concurrency)
    return [p for p in out if p is not None]


def render_page_png(data: bytes, page: int, *, scale: float = 1.5) -> bytes | None:
    """原件第 page 页的预览图（界面左侧原文对照用）。docx / xlsx 没有版面，返回 None。"""
    fmt = sniff(data)
    if fmt == "pdf":
        pdf = pdfium.PdfDocument(data)
        try:
            if not 1 <= page <= len(pdf):
                return None
            p = pdf[page - 1]
            try:
                return _png(p.render(scale=scale).to_pil())
            finally:
                p.close()
        finally:
            pdf.close()
    if fmt == "image":
        img = Image.open(io.BytesIO(data))
        if not 1 <= page <= getattr(img, "n_frames", 1):
            return None
        img.seek(page - 1)
        return _png(img)
    return None


def _png(img: Image.Image) -> bytes:
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _html_table(rows: list[list[str]]) -> str:
    body = "".join("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table>{body}</table>"


def _parse_docx(data: bytes) -> PageResult:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(io.BytesIO(data))
    parts: list[str] = []
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, doc)
            text = p.text.strip()
            if not text:
                continue
            style = (p.style.name if p.style is not None else "") or ""
            if style.startswith("Heading") and style[-1:].isdigit():
                parts.append("#" * min(int(style[-1]), 6) + " " + text)
            elif style == "Title":
                parts.append("# " + text)
            else:
                parts.append(text)
        elif tag == "tbl":
            t = Table(child, doc)
            parts.append(_html_table([[c.text.strip() for c in row.cells] for row in t.rows]))
    md = "\n\n".join(parts)
    return PageResult(page=1, method="docx", markdown=md, data_mode="real", chars=len(md))


def _parse_xlsx(data: bytes, max_pages: int) -> list[PageResult]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        if len(wb.sheetnames) > max_pages:
            raise ParseError(f"工作表 {len(wb.sheetnames)} 个，超过上限 {max_pages}")
        out: list[PageResult] = []
        for i, ws in enumerate(wb.worksheets):
            rows: list[list[str]] = []
            for row in ws.iter_rows(values_only=True):
                cells = ["" if v is None else str(v) for v in row]
                if any(cells):
                    rows.append(cells)
                if len(rows) >= MAX_XLSX_ROWS:
                    break
            width = max((max((j for j, c in enumerate(r) if c), default=-1) for r in rows), default=-1) + 1
            rows = [r[:width] for r in rows]
            md = f"## {ws.title}\n\n" + (_html_table(rows) if rows else "（空表）")
            out.append(
                PageResult(
                    page=i + 1, method="xlsx", markdown=md, data_mode="real", title=ws.title, chars=len(md)
                )
            )
        return out
    finally:
        wb.close()
