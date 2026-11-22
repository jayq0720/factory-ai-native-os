"""经营决策驾驶舱与市场商机三表 Excel 导出（防公式注入、仅真实数据）。"""

from __future__ import annotations

import io
from typing import Any

from fos.excel_safe import excel_row


def market_report_xlsx(
    factory: str,
    cockpit: dict[str, Any],
    leads: list[dict[str, Any]],
    intels: list[dict[str, Any]],
) -> bytes:
    """导出经营决策驾驶舱、市场商机漏斗、竞品行情情报三表 Excel。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    w_c = wb.active
    w_c.title = "经营决策驾驶舱"
    w_c.append(excel_row([f"{factory} 经营决策总览（{cockpit['date']}）"]))
    w_c["A1"].font = Font(bold=True, size=13)
    k = cockpit["kpis"]
    for label, val in (
        ("在跟进商机数", k["market_open_leads"]),
        ("商机加权预测额（元）", k["market_weighted_pipeline"]),
        ("累计赢单金额（元）", k["market_won_amount"]),
        ("商机赢单率（%）", k["market_win_rate_pct"]),
        ("未收应收账款（元）", k["finance_ar_outstanding"]),
        ("未付应付账款（元）", k["finance_ap_outstanding"]),
        ("未闭环质量 NCR 数", k["quality_open_ncr"]),
        ("未完工设备维修工单数", k["eam_open_wo"]),
        ("未销项安全隐患数", k["ehs_open_hazards"]),
        ("未闭环重大隐患数", k["ehs_major_open_hazards"]),
        ("在职员工总数", k["hr_active_headcount"]),
        ("在研/试产研发项目数", k["rnd_active_projects"]),
        ("已定型配方数", k["rnd_approved_formulas"]),
        ("在建筑炉施工项目数", k["construction_active_sites"]),
    ):
        w_c.append(excel_row([label, val]))

    w_l = wb.create_sheet("市场商机漏斗")
    l_cols = ["商机号", "客户", "行业", "来源", "意向产品", "预计金额", "概率%", "加权额", "阶段", "负责人"]
    w_l.append(l_cols)
    for c in w_l[1]:
        c.font = Font(bold=True)
    for ld in leads:
        w_l.append(
            excel_row(
                [
                    ld["lead_no"],
                    ld["customer_name"],
                    ld["industry_text"],
                    ld["source_text"],
                    ld["product_interest"],
                    ld["est_amount"],
                    ld["probability"],
                    ld["weighted_amount"],
                    ld["stage_text"],
                    ld["owner_name"],
                ]
            )
        )

    w_i = wb.create_sheet("竞品行情情报")
    i_cols = ["情报号", "竞争对手", "产品大类", "参考吨价", "区域", "记录日期", "行情摘要"]
    w_i.append(i_cols)
    for c in w_i[1]:
        c.font = Font(bold=True)
    for it in intels:
        w_i.append(
            excel_row(
                [
                    it["intel_no"],
                    it["competitor"],
                    it["product_category"],
                    it.get("quoted_price") if it.get("quoted_price") is not None else "",
                    it.get("region") or "",
                    it.get("recorded_date") or "",
                    it.get("summary") or "",
                ]
            )
        )

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
