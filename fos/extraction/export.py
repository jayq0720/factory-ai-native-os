"""导出抽取结果：Excel（字段表 + 逐页原文 + 说明）和 JSON。模拟数据不导出正式文件（规则 4）。"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import Any

from fos.db.models import Document, DocumentField, DocumentPage

STATUS_TEXT = {"extracted": "待核对", "missing": "缺失", "corrected": "人工修改", "confirmed": "已确认"}
METHOD_TEXT = {
    "rule": "规则",
    "llm": "模型",
    "rule+llm": "规则+模型",
    "manual": "人工",
    "none": "",
    "text_layer": "文字层",
    "ocr": "OCR",
    "ocr_mock": "模拟识别",
    "docx": "Word",
    "xlsx": "Excel",
}
EXCEL_CELL_MAX = 32000


def field_out(f: DocumentField) -> dict[str, Any]:
    return {
        "key": f.key,
        "name": f.name,
        "group": f.group_name,
        "type": f.type,
        "required": f.required,
        "value": f.value,
        "unit": f.unit,
        "status": f.status,
        "method": f.method,
        "confidence": round(f.confidence or 0.0, 2),
        "conflict": f.conflict,
        "sources": f.sources or [],
        "candidates": f.candidates or [],
        "data_mode": f.data_mode,
        "history": f.history or [],
        "updated_at": f.updated_at.isoformat() if f.updated_at else None,
    }


def summary(fields: list[DocumentField]) -> dict[str, int]:
    return {
        "total": len(fields),
        "extracted": sum(f.status == "extracted" for f in fields),
        "missing": sum(f.status == "missing" for f in fields),
        "missing_required": sum(f.status == "missing" and f.required for f in fields),
        "corrected": sum(f.status == "corrected" for f in fields),
        "confirmed": sum(f.status == "confirmed" for f in fields),
        "conflicts": sum(bool(f.conflict) for f in fields),
    }


def to_json(doc: Document, pages: list[DocumentPage], fields: list[DocumentField]) -> dict[str, Any]:
    return {
        "document": {
            "id": doc.id,
            "filename": doc.filename,
            "sha256": doc.sha256,
            "kind": doc.kind,
            "schema_id": doc.schema_id,
            "page_count": doc.page_count,
            "data_mode": doc.data_mode,
        },
        "summary": summary(fields),
        "fields": [field_out(f) for f in fields],
        "pages": [
            {"page": p.page, "method": p.method, "data_mode": p.data_mode, "markdown": p.markdown}
            for p in pages
        ],
        "exported_at": datetime.now(UTC).isoformat(),
    }


def _pages_text(f: DocumentField) -> str:
    return "、".join(str(p) for p in dict.fromkeys(s.get("page") for s in f.sources or []))


def _quotes(f: DocumentField) -> str:
    qs = list(dict.fromkeys(f"[第{s.get('page')}页] {s.get('quote', '')}" for s in f.sources or []))
    return "\n".join(qs[:10])[:EXCEL_CELL_MAX]


def to_xlsx(
    doc: Document,
    pages: list[DocumentPage],
    fields: list[DocumentField],
    *,
    exported_by: str,
    schema_name: str = "",
    group_order: list[str] | None = None,
) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "字段"
    head = ["分组", "字段", "值", "单位", "状态", "必填", "冲突", "可信度", "抽取方式", "页码", "原文出处"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="E8EEF4")
    order = {g: i for i, g in enumerate(group_order or [])}
    warn = PatternFill("solid", fgColor="FDECEC")
    for f in sorted(fields, key=lambda x: order.get(x.group_name, 99)):
        ws.append(
            [
                f.group_name,
                f.name,
                f.value or "",
                f.unit or "",
                STATUS_TEXT.get(f.status, f.status),
                "是" if f.required else "",
                "是" if f.conflict else "",
                round(f.confidence or 0.0, 2) if f.status != "missing" else "",
                METHOD_TEXT.get(f.method, f.method),
                _pages_text(f),
                _quotes(f),
            ]
        )
        if (f.status == "missing" and f.required) or f.conflict:
            for c in ws[ws.max_row]:
                c.fill = warn
    for col, w in zip("ABCDEFGHIJK", (12, 16, 36, 8, 10, 6, 6, 8, 10, 10, 70), strict=True):
        ws.column_dimensions[col].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "C2"

    wp = wb.create_sheet("逐页原文")
    wp.append(["页码", "解析方式", "内容"])
    for c in wp[1]:
        c.font = Font(bold=True)
    for p in pages:
        wp.append([p.page, METHOD_TEXT.get(p.method, p.method), p.markdown[:EXCEL_CELL_MAX]])
    wp.column_dimensions["C"].width = 120
    for row in wp.iter_rows(min_row=2):
        row[2].alignment = Alignment(wrap_text=True, vertical="top")

    wi = wb.create_sheet("说明")
    s = summary(fields)
    for k, v in [
        ("文件名", doc.filename),
        ("SHA-256", doc.sha256),
        ("文件类别", doc.kind),
        ("抽取 Schema", schema_name or doc.schema_id or ""),
        ("页数", doc.page_count),
        ("数据模式", doc.data_mode),
        ("字段数", s["total"]),
        ("缺失（其中必填）", f"{s['missing']}（{s['missing_required']}）"),
        ("冲突", s["conflicts"]),
        ("人工修改 / 已确认", f"{s['corrected']} / {s['confirmed']}"),
        ("导出人", exported_by),
        ("导出时间（UTC）", datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")),
        ("说明", "每个字段都标了原文页码与片段；缺失和冲突行标红，请在使用前人工核对。"),
    ]:
        wi.append([k, v])
    wi.column_dimensions["A"].width = 18
    wi.column_dimensions["B"].width = 80
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
