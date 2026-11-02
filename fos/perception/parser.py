"""文档解析管线（方案 4.2 第 2 步）。

- PDF：逐页判断。有文字层直接取字（快、准）；没有文字层的扫描页渲染成 PNG 交给 OCR。
- 图片（PNG / JPEG / TIFF）：转 PNG 后交给 OCR；多帧 TIFF 每帧一页。
- Word（.docx）：按正文顺序输出段落与表格，作为一页。
- Excel（.xlsx）：每个工作表一页，输出 HTML 表格（与 OCR 的表格格式一致）。

每页记录解析方式和 data_mode，模拟 OCR 的页会让整份文档变成 mock。
"""

from __future__ import annotations

import html
import io
import zipfile

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


async def parse(
    data: bytes, ocr: OCRProvider, *, max_pages: int = 300, render_scale: float = 2.0
) -> ParsedDocument:
    fmt = sniff(data)
    if fmt == "pdf":
        pages = await _parse_pdf(data, ocr, max_pages, render_scale)
    elif fmt == "image":
        pages = await _parse_image(data, ocr, max_pages)
    elif fmt == "docx":
        pages = [_parse_docx(data)]
    else:
        pages = _parse_xlsx(data, max_pages)
    return ParsedDocument(format=fmt, pages=pages)


def _ocr_method(ocr: OCRProvider) -> str:
    return "ocr_mock" if ocr.data_mode == "mock" else "ocr"


async def _parse_pdf(data: bytes, ocr: OCRProvider, max_pages: int, scale: float) -> list[PageResult]:
    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as e:
        raise ParseError(f"PDF 无法打开：{e}") from e
    try:
        n = len(pdf)
        if n > max_pages:
            raise ParseError(f"PDF 共 {n} 页，超过上限 {max_pages} 页")
        out: list[PageResult] = []
        for i in range(n):
            page = pdf[i]
            try:
                tp = page.get_textpage()
                text = tp.get_text_range().replace("\r\n", "\n").replace("\r", "\n")
                tp.close()
                if sum(not c.isspace() for c in text) >= TEXT_LAYER_MIN_CHARS:
                    out.append(
                        PageResult(
                            page=i + 1,
                            method="text_layer",
                            markdown=text.strip(),
                            data_mode="real",
                            chars=len(text),
                        )
                    )
                    continue
                img = page.render(scale=scale).to_pil()
                md = await ocr.recognize(_png(img), page=i + 1)
                out.append(
                    PageResult(
                        page=i + 1,
                        method=_ocr_method(ocr),
                        markdown=md,
                        data_mode=ocr.data_mode,
                        chars=len(md),
                    )
                )
            finally:
                page.close()
        return out
    finally:
        pdf.close()


async def _parse_image(data: bytes, ocr: OCRProvider, max_pages: int) -> list[PageResult]:
    try:
        img = Image.open(io.BytesIO(data))
    except (UnidentifiedImageError, Image.DecompressionBombError) as e:
        raise ParseError(f"图片无法读取：{e}") from e
    frames = getattr(img, "n_frames", 1)
    if frames > max_pages:
        raise ParseError(f"图片共 {frames} 帧，超过上限 {max_pages}")
    out: list[PageResult] = []
    for i in range(frames):
        img.seek(i)
        md = await ocr.recognize(_png(img), page=i + 1)
        out.append(
            PageResult(
                page=i + 1, method=_ocr_method(ocr), markdown=md, data_mode=ocr.data_mode, chars=len(md)
            )
        )
    return out


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
