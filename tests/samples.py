"""测试用样例文件，全部在内存里生成，不依赖外部文件。"""

from __future__ import annotations

import io


def pdf(pages: list[str | None]) -> bytes:
    """最小 PDF。字符串页带文字层（Helvetica，ASCII），None 页是没有文字的“扫描页”（画一个方块）。"""
    objs: list[bytes] = []
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    font_id = 3 + 2 * n
    for i, text in enumerate(pages):
        content_id = 4 + 2 * i
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        if text is None:
            stream = b"0.2 g 72 600 200 120 re f"
        else:
            lines = []
            for j, line in enumerate(text.split("\n")):
                safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                lines.append(f"BT /F1 11 Tf 72 {780 - 16 * j} Td ({safe}) Tj ET")
            stream = "\n".join(lines).encode()
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for k, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{k} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def png(color: tuple[int, int, int] = (200, 190, 170)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, format="PNG")
    return buf.getvalue()


def docx() -> bytes:
    from docx import Document

    d = Document()
    d.add_heading("2# 辊道窑烧成带炉衬改造 技术要求", level=1)
    d.add_paragraph("2.1 烧成带正常工作温度 1250℃，最高温度 1300℃。")
    t = d.add_table(rows=2, cols=3)
    for r, row in enumerate([["部位", "尺寸", "面积"], ["炉墙（两侧）", "36 m × 2.40 m × 2", "172.8 m²"]]):
        for c, v in enumerate(row):
            t.cell(r, c).text = v
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "工程量清单"
    ws.append(["序号", "部位", "面积", "单位"])
    ws.append([1, "炉墙", 172.8, "m²"])
    ws.append([2, "炉顶", 100.8, "m²"])
    wb.create_sheet("说明").append(["按 1300℃ 校核"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
