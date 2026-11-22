"""市场商机与经营决策驾驶舱 IM 卡片与 Fast Path 关键词（零模型成本）。"""

from __future__ import annotations

import re
from typing import Any

from fos.channels.cards import Card, CardField

COCKPIT = re.compile(r"^(?:经营|驾驶舱|经营驾驶舱|经营总览)$")
FUNNEL = re.compile(r"^(?:商机|线索|商机漏斗|市场)$")
INTEL = re.compile(r"^(?:竞品|行情|竞品情报)$")
MONTHLY = re.compile(r"^(?:月报|经营月报|月度报表)$")


def cockpit_card(cockpit: dict[str, Any]) -> Card:
    k = cockpit["kpis"]
    rows = [
        ("市场商机", f"在跟 {k['market_open_leads']} · 加权 {k['market_weighted_pipeline']:g}元"),
        ("财务往来", f"应收 {k['finance_ar_outstanding']:g} / 应付 {k['finance_ap_outstanding']:g}元"),
        ("质量设备", f"未闭环NCR {k['quality_open_ncr']} · 未完修工单 {k['eam_open_wo']}"),
        ("EHS安全", f"未销项隐患 {k['ehs_open_hazards']}（重大 {k['ehs_major_open_hazards']}）"),
        ("人力研发", f"在职 {k['hr_active_headcount']}人 · 在研 {k['rnd_active_projects']}项"),
        ("筑炉施工", f"在建施工项目 {k['construction_active_sites']}项"),
    ]
    fields = [CardField(label=lb, value=val) for lb, val in rows]
    d = cockpit["date"]
    return Card(
        title="工厂经营决策驾驶舱",
        text=f"{d} 全厂跨部门经营总览（实时账本确定性汇总）。",
        fields=fields,
        data_mode=cockpit["data_mode"],
        footer="详见控制台 /app/market.html",
    )


def funnel_card(board: dict[str, Any]) -> Card:
    m = board["metrics"]
    fields = [
        CardField(
            label=f"{it['lead_no']} {it['customer_name']}"[:20],
            value=f"{it['stage_text']} · {it['est_amount']:g}元（{it['probability']}%）",
        )
        for it in board["items"][:6]
    ]
    w = m["weighted_pipeline_amount"]
    text = f"共 {m['total']} 条：在跟 {m['open_count']}，加权预测 {w:g} 元，赢单率 {m['win_rate_pct']}%。"
    return Card(
        title="市场商机漏斗看板",
        text=text,
        fields=fields,
        data_mode=board["data_mode"],
        footer="详见控制台 /app/market.html",
    )


def intel_card(board: dict[str, Any]) -> Card:
    c = board["counts"]
    fields = [
        CardField(
            label=f"{it['competitor']} {it['product_category']}"[:20],
            value=f"{it['recorded_date']} · {it.get('quoted_price') or '未报价'}元/吨",
        )
        for it in board["items"][:6]
    ]
    return Card(
        title="竞品行情情报看板",
        text=f"共收录 {c['total']} 条竞品报价与区域行情。",
        fields=fields,
        data_mode=board["data_mode"],
        footer="详见控制台 /app/market.html",
    )


def monthly_card(report: dict[str, Any]) -> Card:
    k = report["kpis"]
    act = report["activity"]
    ar, ap = k.get("finance_ar_outstanding", 0), k.get("finance_ap_outstanding", 0)
    pipe, won = k.get("market_weighted_pipeline", 0), k.get("market_won_amount", 0)
    rows = [
        ("财务往来", f"应收 {ar:g} / 应付 {ap:g}元"),
        ("市场商机", f"加权 {pipe:g} / 赢单 {won:g}元"),
        ("质量设备", f"未闭环NCR {k.get('quality_open_ncr', 0)} · 工单 {k.get('eam_open_wo', 0)}"),
        ("安全隐患", f"未闭环 {k.get('ehs_open_hazards', 0)} 项"),
        ("月度运行", f"事件 {act['events_total']} 条 · 待办闭环率 {act['todo_close_rate_pct']}%"),
        ("风险项数", f"{len(report.get('risks') or [])} 项"),
    ]
    return Card(
        title=f"{report['factory_name']} · {report['month']} 经营月报",
        text="仅统计真实账本数据（支持控制台一键导出三表 Excel）。",
        fields=[CardField(label=lb, value=val) for lb, val in rows],
        data_mode="real",
        footer="详见控制台 /app/market.html",
    )
