"""财务与成本 IM 卡片与 Fast Path 关键词（零模型成本）。"""

from __future__ import annotations

import re
from typing import Any

from fos.channels.cards import Card, CardField

AR_SUMMARY = re.compile(r"^(?:应收|应收账款|回款|回款进度|欠款)$")
AP_SUMMARY = re.compile(r"^(?:应付|应付账款|付款|应付汇总)$")
BATCH_COST = re.compile(r"^(?:成本|批次成本)\s+([A-Za-z0-9_-]{2,64})$")
VOUCHERS = re.compile(r"^(?:凭证|凭证草稿)$")


def ar_card(board: dict[str, Any]) -> Card:
    s = board["summary"]
    ag = s["aging"]
    over60 = ag["61_90d"] + ag["over_90d"]
    fields = [
        CardField(label="应收总额", value=f"¥{s['total_amount']:,.2f}"),
        CardField(label="已回款", value=f"¥{s['total_received']:,.2f}"),
        CardField(label="未收余额", value=f"¥{s['total_balance']:,.2f}"),
        CardField(label="逾期笔数 / 金额", value=f"{s['overdue_count']} 笔 / ¥{s['overdue_balance']:,.2f}"),
        CardField(
            label="逾期账龄",
            value=f"1-30天 ¥{ag['1_30d']:,.0f} · 31-60天 ¥{ag['31_60d']:,.0f} · >60天 ¥{over60:,.0f}",
        ),
    ]
    return Card(
        title="应收账款汇总",
        text=f"截至 {board['date']}，共 {s['count']} 笔应收单。",
        fields=fields,
        data_mode=board["data_mode"],
        footer="详见控制台 /app/finance.html",
    )


def ap_card(board: dict[str, Any]) -> Card:
    s = board["summary"]
    fields = [
        CardField(label="应付总额", value=f"¥{s['total_amount']:,.2f}"),
        CardField(label="已付款", value=f"¥{s['total_paid']:,.2f}"),
        CardField(label="未付余额", value=f"¥{s['total_balance']:,.2f}"),
        CardField(label="逾期笔数 / 金额", value=f"{s['overdue_count']} 笔 / ¥{s['overdue_balance']:,.2f}"),
    ]
    return Card(
        title="应付账款汇总",
        text=f"截至 {board['date']}，共 {s['count']} 笔应付单。",
        fields=fields,
        data_mode=board["data_mode"],
        footer="大额付款（≥5万元）自动走总经理审批",
    )


def batch_cost_card(rep: dict[str, Any]) -> Card:
    uc = f"¥{rep['unit_cost']:,.4f}/{rep['unit']}" if rep.get("unit_cost") is not None else "—"
    fields = [
        CardField(label="产品 / 数量", value=f"{rep['product_name']} · {rep['qty']:g} {rep['unit']}"),
        CardField(label="直接材料", value=f"¥{rep['material_cost']:,.2f}"),
        CardField(label="窑炉能耗", value=f"¥{rep['energy_cost']:,.2f}"),
        CardField(label="人工 + 制造费用", value=f"¥{rep['labor_cost'] + rep['overhead_cost']:,.2f}"),
        CardField(label="总成本 / 单位成本", value=f"¥{rep['total_cost']:,.2f}（{uc}）"),
    ]
    warn = "；".join(rep["warnings"][:2]) if rep.get("warnings") else "全部成本要素齐全。"
    return Card(
        title=f"批次成本 {rep['batch_id']}",
        text=warn[:300],
        fields=fields,
        data_mode=rep.get("data_mode", "real"),
        footer="确定性核算 · 可复算",
    )


def vouchers_card(rows: list[dict[str, Any]]) -> Card:
    draft_n = sum(1 for r in rows if r["status"] == "draft")
    exp_n = sum(1 for r in rows if r["status"] == "exported")
    post_n = sum(1 for r in rows if r["status"] == "posted")
    fields = [
        CardField(
            label=r["voucher_no"][:20],
            value=f"{r['status_text']} · ¥{r['total_debit']:,.2f} · {r['summary'][:30]}",
        )
        for r in rows[:6]
    ]
    return Card(
        title="凭证草稿台账",
        text=f"共 {len(rows)} 张（草稿 {draft_n} / 已导出 {exp_n} / 已过账 {post_n}）。",
        fields=fields,
        footer="可在 /app/finance.html 导出金蝶/用友 Excel",
    )
