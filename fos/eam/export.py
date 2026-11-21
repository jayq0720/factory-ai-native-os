"""设备管理（EAM）三表汇总导出（Excel，防公式注入、仅真实数据）。"""

from __future__ import annotations

import io
from typing import Any

from fos.excel_safe import excel_row


def eam_report_xlsx(
    factory: str,
    equip_board: dict[str, Any],
    work_orders: list[dict[str, Any]],
    reliability: dict[str, Any],
) -> bytes:
    """导出设备台账、维修工单与可靠性 KPI 三表 Excel。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "设备台账"
    ws.append(excel_row([f"{factory} 设备台账与运行状态"]))
    ws["A1"].font = Font(bold=True, size=13)
    eq_cols = ["编码", "名称", "类别", "型号", "位置", "关键等级", "状态", "下次保养日"]
    ws.append(eq_cols)
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    for it in equip_board.get("items") or []:
        ws.append(
            excel_row(
                [
                    it["code"],
                    it["name"],
                    it["category_text"],
                    it.get("model") or "",
                    it.get("location") or "",
                    it["criticality"],
                    it["status_text"],
                    it.get("next_pm_date") or "",
                ]
            )
        )

    w_wo = wb.create_sheet("维修工单")
    wo_cols = ["工单号", "设备", "类型", "优先级", "状态", "停机(分)", "故障现象", "处理措施"]
    w_wo.append(wo_cols)
    for c in w_wo[1]:
        c.font = Font(bold=True)
    for w in work_orders:
        w_wo.append(
            excel_row(
                [
                    w["wo_no"],
                    w["equipment_name"],
                    w["kind_text"],
                    w["priority_text"],
                    w["status_text"],
                    w["downtime_minutes"],
                    w.get("symptom") or "",
                    w.get("action_taken") or "",
                ]
            )
        )

    w_kpi = wb.create_sheet("可靠性KPI")
    m = reliability["metrics"]
    for label, val in (
        ("统计窗口（天）", m["window_days"]),
        ("计划运行时长（分钟）", m["planned_minutes"]),
        ("故障次数", m["breakdown_count"]),
        ("总停机时长（分钟）", m["total_downtime_minutes"]),
        ("设备可用率（%）", m["availability_pct"]),
        ("平均修复时间 MTTR（分钟）", m["mttr_minutes"] if m["mttr_minutes"] is not None else "—"),
        ("平均故障间隔 MTBF（小时）", m["mtbf_hours"] if m["mtbf_hours"] is not None else "—"),
    ):
        w_kpi.append(excel_row([label, val]))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
