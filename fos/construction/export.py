"""筑炉施工与烘炉验收三表 Excel 导出（防公式注入、仅真实数据）。"""

from __future__ import annotations

import io
from typing import Any

from fos.excel_safe import excel_row


def construction_report_xlsx(
    factory: str,
    sites: list[dict[str, Any]],
    logs: list[dict[str, Any]],
    dryouts: list[dict[str, Any]],
) -> bytes:
    """导出筑炉施工项目、施工日报台账、烘炉升温记录三表 Excel。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    w_s = wb.active
    w_s.title = "筑炉施工项目"
    w_s.append(excel_row([f"{factory} 筑炉施工与烘炉验收台账"]))
    w_s["A1"].font = Font(bold=True, size=13)
    s_cols = ["项目号", "工程名称", "客户", "炉型", "负责人", "计划吨", "完成吨", "进度%", "状态"]
    w_s.append(s_cols)
    for c in w_s[w_s.max_row]:
        c.font = Font(bold=True)
    for s in sites:
        w_s.append(
            excel_row(
                [
                    s["site_no"],
                    s["name"],
                    s["customer_name"],
                    s["furnace_type"],
                    s["supervisor"],
                    s["planned_tons"],
                    s["completed_tons"],
                    s["progress_pct"],
                    s["status_text"],
                ]
            )
        )

    w_l = wb.create_sheet("施工日报台账")
    l_cols = ["日报号", "工程名称", "日期", "人数", "当日吨位", "天气", "安全", "施工内容"]
    w_l.append(l_cols)
    for c in w_l[1]:
        c.font = Font(bold=True)
    for lg in logs:
        w_l.append(
            excel_row(
                [
                    lg["log_no"],
                    lg["site_name"],
                    lg.get("log_date") or "",
                    lg["crew_size"],
                    lg["tons_installed"],
                    lg.get("weather") or "",
                    "正常" if lg["safety_ok"] else "异常",
                    lg.get("summary") or "",
                ]
            )
        )

    w_d = wb.create_sheet("烘炉升温记录")
    d_cols = ["记录号", "工程名称", "阶段", "目标℃", "实测℃", "速率℃/h", "判定", "测温时间"]
    w_d.append(d_cols)
    for c in w_d[1]:
        c.font = Font(bold=True)
    for d in dryouts:
        w_d.append(
            excel_row(
                [
                    d["dryout_no"],
                    d["site_name"],
                    d["stage_name"],
                    d["target_temp_c"],
                    d["actual_temp_c"],
                    d["ramp_rate_c_per_h"],
                    "正常" if d["within_spec"] else "告警",
                    d.get("recorded_at") or "",
                ]
            )
        )

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
