"""质保书（Word / Excel）与 ISO 9001 审核证据包（Excel）导出。"""

from __future__ import annotations

import io
from typing import Any

from fos.excel_safe import excel_row, xml_safe


def certificate_docx(factory: str, coa: dict[str, Any]) -> bytes:
    """生成正式质保书 Word 文档（.docx）。"""
    from docx import Document

    doc = Document()
    doc.add_heading(xml_safe(f"{factory} {coa['title']}"), level=1)
    meta = [
        ("质保书编号", coa["certificate_no"]),
        ("生产批号", coa["batch_id"]),
        ("产品牌号 / 名称", f"{coa['product_code']} {coa['product_name']}".strip()),
        ("数量", f"{coa['qty']} {coa['unit']}".strip() if coa.get("qty") is not None else "—"),
        ("客户 / 订单", f"{coa['customer']} {coa['order_no']}".strip() or "—"),
        ("烧成窑次", coa.get("kiln_run") or "—"),
        ("检验报告编号", coa["report_no"]),
        ("执行标准", coa["standard_ref"]),
        ("检验结论", coa["result"]),
    ]
    t1 = doc.add_table(rows=len(meta), cols=2)
    t1.style = "Table Grid"
    for i, (k, v) in enumerate(meta):
        t1.rows[i].cells[0].text = xml_safe(k)
        t1.rows[i].cells[1].text = xml_safe(str(v))

    doc.add_heading("理化性能检验结果", level=2)
    checks = coa.get("checks") or []
    t2 = doc.add_table(rows=1 + len(checks), cols=5)
    t2.style = "Table Grid"
    for j, h in enumerate(("检验项目", "实测值", "单位", "标准要求", "单项结论")):
        t2.rows[0].cells[j].text = h
    for i, c in enumerate(checks, 1):
        vals = (c["name"], f"{c['value']:g}", c["unit"], c["note"], "合格" if c["ok"] else "不合格")
        for j, v in enumerate(vals):
            t2.rows[i].cells[j].text = xml_safe(str(v))

    doc.add_paragraph(xml_safe(coa.get("footer_note") or ""))
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def certificate_xlsx(factory: str, coa: dict[str, Any]) -> bytes:
    """生成正式质保书 Excel（.xlsx），防公式注入。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "质保书"
    ws.append(excel_row([f"{factory} {coa['title']}"]))
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])
    for k, v in (
        ("质保书编号", coa["certificate_no"]),
        ("生产批号", coa["batch_id"]),
        ("产品", f"{coa['product_code']} {coa['product_name']}".strip()),
        ("数量", f"{coa['qty']} {coa['unit']}".strip() if coa.get("qty") is not None else ""),
        ("客户", coa.get("customer") or ""),
        ("订单号", coa.get("order_no") or ""),
        ("窑次", coa.get("kiln_run") or ""),
        ("检验报告号", coa["report_no"]),
        ("执行标准", coa["standard_ref"]),
        ("综合结论", coa["result"]),
    ):
        ws.append(excel_row([k, v]))
    ws.append([])
    ws.append(["检验项目", "实测值", "单位", "标准与判定", "结果"])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    for ch in coa.get("checks") or []:
        verdict = "合格" if ch["ok"] else "不合格"
        ws.append(excel_row([ch["name"], ch["value"], ch["unit"], ch["note"], verdict]))
    ws.append([])
    ws.append(excel_row(["说明", coa.get("footer_note") or ""]))
    for col, w in zip("ABCDE", (20, 14, 12, 34, 10), strict=True):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def iso_audit_xlsx(factory: str, pack: dict[str, Any]) -> bytes:
    """导出 ISO 9001 审核证据汇总表（KPI + NCR 台账 + 计量器具台账 + 账本证据索引）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "质量KPI汇总"
    p = pack["period"]
    ws.append(excel_row([f"{factory} ISO 9001 质量审核证据包（{p['start']} 至 {p['end']}）"]))
    ws["A1"].font = Font(bold=True, size=13)
    k = pack["kpis"]
    for label, val in (
        ("检验总数", k["inspections_total"]),
        ("来料检验合格率（%）", k["iqc_pass_rate"] if k["iqc_pass_rate"] is not None else "—"),
        ("出厂检验合格率（%）", k["fqc_pass_rate"] if k["fqc_pass_rate"] is not None else "—"),
        ("NCR 开启数", k["ncr_opened"]),
        ("NCR 关闭数", k["ncr_closed"]),
        ("NCR 关闭率（%）", k["ncr_close_rate"] if k["ncr_close_rate"] is not None else "—"),
        ("需 8D 整改数 / 已完成", f"{k['d8_required']} / {k['d8_completed']}"),
        ("在用计量器具数", k["instruments_active"]),
        (
            "计量器具按期校准率（%）",
            k["calibration_on_time_rate"] if k["calibration_on_time_rate"] is not None else "—",
        ),
    ):
        ws.append(excel_row([label, val]))

    w_ncr = wb.create_sheet("不合格品NCR")
    w_ncr.append(["NCR编号", "来源", "严重程度", "状态", "处置方式", "8D状态", "问题描述", "根本原因"])
    for n in pack["ncrs"]:
        w_ncr.append(
            excel_row(
                [
                    n["ncr_no"],
                    n["source_text"],
                    n["severity_text"],
                    n["status_text"],
                    n["disposition_text"],
                    n["d8_text"],
                    n["summary"] or "",
                    n["root_cause"] or "",
                ]
            )
        )

    w_inst = wb.create_sheet("计量器具校准")
    w_inst.append(["编号", "名称", "类别", "位置", "上次校准", "下次到期", "状态", "证书号"])
    for it in pack["instruments"]:
        w_inst.append(
            excel_row(
                [
                    it["code"],
                    it["name"],
                    it["category_text"],
                    it["location"] or "",
                    it["last_calibrated_on"] or "",
                    it["next_due_date"] or "",
                    it["cal_text"],
                    it["certificate_no"] or "",
                ]
            )
        )

    w_ev = wb.create_sheet("账本证据索引")
    w_ev.append(["事件序号", "事件类型", "对象编号", "发生时间"])
    for ev in pack["evidence_events"]:
        w_ev.append(excel_row([ev["seq"], ev["type"], ev["object_id"], ev["at"]]))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
