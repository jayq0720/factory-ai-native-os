"""研发配方与试验三表 Excel 导出（防公式注入、仅真实数据）。"""

from __future__ import annotations

import io
from typing import Any

from fos.excel_safe import excel_row


def rnd_report_xlsx(
    factory: str,
    projects: list[dict[str, Any]],
    formulas: list[dict[str, Any]],
    trials: list[dict[str, Any]],
) -> bytes:
    """导出研发项目台账、配方版本与成本、试验检测记录三表 Excel。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    w_p = wb.active
    w_p.title = "研发项目台账"
    w_p.append(excel_row([f"{factory} 研发项目与新产品试产台账"]))
    w_p["A1"].font = Font(bold=True, size=13)
    p_cols = ["项目编号", "项目名称", "目标产品", "Al2O3≥", "体密≥", "耐压≥", "负责人", "结题日", "状态"]
    w_p.append(p_cols)
    for c in w_p[w_p.max_row]:
        c.font = Font(bold=True)
    for p in projects:
        w_p.append(
            excel_row(
                [
                    p["project_no"],
                    p["name"],
                    p["target_product"],
                    p.get("target_al2o3_min") if p.get("target_al2o3_min") is not None else "",
                    p.get("target_bd_min") if p.get("target_bd_min") is not None else "",
                    p.get("target_ccs_min") if p.get("target_ccs_min") is not None else "",
                    p["owner_name"],
                    p.get("due_date") or "",
                    p["status_text"],
                ]
            )
        )

    w_f = wb.create_sheet("配方版本与成本")
    f_cols = ["配方编号", "所属项目", "版本号", "总配比%", "理论Al2O3%", "吨成本(元)", "烧成(℃)", "状态"]
    w_f.append(f_cols)
    for c in w_f[1]:
        c.font = Font(bold=True)
    for f in formulas:
        w_f.append(
            excel_row(
                [
                    f["formula_no"],
                    f["project_name"],
                    f["version_code"],
                    f["total_pct"],
                    f.get("calc_al2o3_pct") if f.get("calc_al2o3_pct") is not None else "",
                    f.get("est_cost_per_ton") if f.get("est_cost_per_ton") is not None else "",
                    f.get("firing_temp_c") if f.get("firing_temp_c") is not None else "",
                    f["status_text"],
                ]
            )
        )

    w_t = wb.create_sheet("试验检测记录")
    t_cols = ["试验单号", "配方版本", "阶段", "日期", "Al2O3%", "体密", "耐压MPa", "热震次", "判定", "结论"]
    w_t.append(t_cols)
    for c in w_t[1]:
        c.font = Font(bold=True)
    for t in trials:
        w_t.append(
            excel_row(
                [
                    t["trial_no"],
                    t["formula_code"],
                    t["stage_text"],
                    t.get("trial_date") or "",
                    t.get("al2o3_pct") if t.get("al2o3_pct") is not None else "",
                    t.get("bulk_density") if t.get("bulk_density") is not None else "",
                    t.get("ccs_mpa") if t.get("ccs_mpa") is not None else "",
                    t.get("thermal_shock_cycles") if t.get("thermal_shock_cycles") is not None else "",
                    "达标" if t["passed"] else "不达标",
                    t.get("conclusion") or "",
                ]
            )
        )

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
