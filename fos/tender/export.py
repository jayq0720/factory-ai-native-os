"""招标拆解导出 Excel：需求矩阵、偏离表、资料清单、评分点（投标小组线下分工用）。"""

from __future__ import annotations

import io
from typing import Any

from fos.tender.service import LEVEL_TEXT

STATUS_TEXT = {"open": "未处理", "responded": "已响应", "accepted": "接受偏离", "na": "不适用"}
DOC_TEXT = {"prepared": "已备齐", "na": "不适用"}


def to_xlsx(tender: dict[str, Any], analysis: dict[str, Any]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    wb = Workbook()
    ws = wb.active
    ws.title = "需求矩阵"
    ws.append(
        [f"{tender.get('name', '')}  招标编号 {tender.get('tender_no') or '-'}  拆解 v{analysis['version']}"]
    )
    ws.append(
        [
            "条款号",
            "页码",
            "章节",
            "类别",
            "级别",
            "可能废标",
            "原文",
            "偏离",
            "建议响应",
            "负责人",
            "状态",
            "备注",
        ]
    )
    for it in analysis["items"]:
        dev = it.get("deviation") or {}
        ws.append(
            [
                it["no"],
                it["page"],
                it["section_name"],
                it["category"],
                LEVEL_TEXT.get(it["level"], it["level"]),
                "是" if it["disqualify"] else "",
                it["text"],
                dev.get("status", ""),
                it["response"],
                it.get("assignee") or it.get("owner_role") or "",
                STATUS_TEXT.get(it["status"], it["status"]),
                it.get("note") or "",
            ]
        )
    ws2 = wb.create_sheet("偏离表")
    ws2.append(["条款号", "指标", "招标要求", "我方", "依据", "偏离", "原文"])
    for d in analysis["deviations"]:
        ws2.append(
            [
                d["no"],
                d["name"],
                d["required"],
                d.get("ours"),
                d["basis"],
                d["status"],
                d["source"].get("quote", ""),
            ]
        )
    ws3 = wb.create_sheet("资料清单")
    ws3.append(["资料", "页码", "要求", "资料库匹配", "状态", "处理"])
    for d in analysis["checklist"]:
        req = []
        if d.get("max_age_months") is not None:
            req.append(f"{d['max_age_months']:g} 个月内")
        if d.get("min_count"):
            req.append(f"不少于 {d['min_count']} 份")
        matches = "；".join(
            f"{m['name']}（{m.get('valid_until') or '长期'}）{m.get('problem') or ''}" for m in d["matches"]
        )
        ws3.append(
            [
                ("★ " if d.get("star") else "") + d["name"],
                d["sources"][0]["page"] if d["sources"] else "",
                "，".join(req),
                matches,
                d["status_text"],
                DOC_TEXT.get(d.get("disposition") or "", ""),
            ]
        )
    ws4 = wb.create_sheet("评分点")
    ws4.append(["得分项", "分值", "页码", "需要的证明材料"])
    names = {d["id"]: d["name"] for d in analysis["checklist"]}
    for s in analysis["scoring"]:
        ws4.append([s["item"], s["points"], s["page"], "、".join(names.get(e, e) for e in s["evidence"])])
    for sheet, widths in (
        (ws, [8, 6, 12, 8, 10, 8, 60, 8, 40, 12, 10, 20]),
        (ws2, [10, 14, 16, 10, 40, 8, 50]),
    ):
        for i, w in enumerate(widths):
            sheet.column_dimensions[chr(65 + i)].width = w
    for row in ws.iter_rows(min_row=3):
        row[6].alignment = row[8].alignment = Alignment(wrap_text=True, vertical="top")
    ws["A1"].font = Font(bold=True)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
