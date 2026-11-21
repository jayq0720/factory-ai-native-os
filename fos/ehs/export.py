"""EHS 安全环保四表合规导出（Excel，防公式注入、仅真实数据）。"""

from __future__ import annotations

import io
from typing import Any

from fos.excel_safe import excel_row


def ehs_report_xlsx(
    factory: str,
    summary: dict[str, Any],
    hazards: list[dict[str, Any]],
    permits: list[dict[str, Any]],
    env_rows: list[dict[str, Any]],
) -> bytes:
    """导出 EHS 合规汇总、安全隐患台账、特种作业票、环保监测记录四表 Excel。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "EHS合规汇总"
    ws.append(excel_row([f"{factory} EHS 安全环保合规报告（{summary['date']}）"]))
    ws["A1"].font = Font(bold=True, size=13)
    k = summary["kpis"]
    for label, val in (
        ("隐患总数", k["hazards_total"]),
        ("未销项隐患", k["hazards_open"]),
        ("超期隐患", k["hazards_overdue"]),
        ("未闭环重大隐患", k["hazards_major_open"]),
        ("隐患闭环率（%）", k["hazard_closure_rate_pct"]),
        ("特种作业票总数", k["permits_total"]),
        ("作业中票数", k["permits_active"]),
        ("高风险作业票（动火/受限空间）", k["permits_high_risk"]),
        ("事故与险情总数", k["incidents_total"]),
        ("累计受伤人数", k["total_injured"]),
        ("环保监测次数", k["env_total"]),
        ("环保超标次数", k["env_exceeded"]),
        ("环保达标率（%）", k["env_compliance_rate_pct"]),
    ):
        ws.append(excel_row([label, val]))

    w_hz = wb.create_sheet("安全隐患台账")
    w_hz.append(["隐患编号", "位置", "类别", "等级", "状态", "整改期限", "隐患描述", "整改措施", "复查说明"])
    for c in w_hz[1]:
        c.font = Font(bold=True)
    for h in hazards:
        w_hz.append(
            excel_row(
                [
                    h["hazard_no"],
                    h["location"],
                    h["category_text"],
                    h["level_text"],
                    h["status_text"],
                    h.get("due_date") or "",
                    h.get("description") or "",
                    h.get("rectification_plan") or "",
                    h.get("verified_note") or "",
                ]
            )
        )

    w_pt = wb.create_sheet("特种作业票")
    w_pt.append(["作业票号", "作业类型", "地点", "负责人", "状态", "有效起时", "有效止时", "安全措施"])
    for c in w_pt[1]:
        c.font = Font(bold=True)
    for p in permits:
        w_pt.append(
            excel_row(
                [
                    p["permit_no"],
                    p["kind_text"],
                    p["location"],
                    p["worker_name"],
                    p["status_text"],
                    p.get("valid_from") or "",
                    p.get("valid_until") or "",
                    p.get("safety_measures") or "",
                ]
            )
        )

    w_env = wb.create_sheet("环保监测记录")
    w_env.append(["监测点位", "指标", "实测值", "限值", "单位", "是否达标", "监测时间", "备注"])
    for c in w_env[1]:
        c.font = Font(bold=True)
    for e in env_rows:
        w_env.append(
            excel_row(
                [
                    e["point_name"],
                    e["kind_text"],
                    e["value"],
                    e["limit_value"],
                    e["unit"],
                    "达标" if e["passed"] else "超标",
                    e.get("measured_at") or "",
                    e.get("note") or "",
                ]
            )
        )

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
