"""IM 卡片：「订单 SO-…」回订单进度，「交期风险」「订单看板」回看板（只读 Fast Path，零模型成本）。"""

from __future__ import annotations

import re
from typing import Any

from fos.channels.cards import Card, CardField

# 「订单 SO-261107-1A2B」「订单：HT2026-018」，编号里至少有一个数字
ORDER = re.compile(r"^(?:@\S+\s+)*(?:订单|查订单|订单进度)\s*[:：]?\s*([\w\-.]*\d[\w\-.]*)$")
BOARD = re.compile(r"^(?:@\S+\s+)*(交期风险|订单看板|在手订单)$")
STATUS_TEXT = {"in_transit": "在途", "delivered": "已签收", "refused": "拒收"}


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def order_card(p: dict[str, Any]) -> Card:
    q, u = p["quantities"], p["unit"] or ""
    fields = [
        CardField(label="客户", value=_cut(p["customer"]["name"] or "", 200)),
        CardField(
            label="产品",
            value=_cut(f"{p['product']['code'] or ''} {p['product']['name'] or ''}".strip(), 200),
        ),
        CardField(label="数量", value=f"{p['qty']:g} {u}"),
        CardField(
            label="交期",
            value=(p["due_date"] or "未填")
            + (
                f"（还有 {p['days_to_due']} 天）"
                if p["days_to_due"] is not None and p["days_to_due"] >= 0
                else ""
            ),
        ),
        *([CardField(label="承诺交期", value=p["promised_date"])] if p.get("promised_date") else []),
        CardField(label="进度", value=p["stage_text"]),
        CardField(
            label="数量进度",
            value=f"排产 {q['planned']:g}｜完工 {q['produced']:g}｜入库 {q['stocked']:g}｜"
            f"发货 {q['shipped']:g}｜签收 {q['delivered']:g}",
        ),
    ]
    if p["contract"]:
        fields.insert(0, CardField(label="合同", value=p["contract"]["contract_no"] or p["contract"]["id"]))
    for sh in (p["shipments"] or [])[-3:]:
        fields.append(
            CardField(
                label=_cut(f"发货 {sh['shipment_no']}", 20),
                value=f"{sh['qty']:g} {sh['unit']}，{STATUS_TEXT.get(sh['status'], sh['status'])}",
            )
        )
    text = "\n".join(f"- **{r['text']}**" if r["level"] == "high" else f"- {r['text']}" for r in p["risks"])
    return Card(
        title=_cut(f"订单 {p['order_no']}", 40),
        text=_cut(text, 500),
        fields=fields[:12],
        data_mode=p["data_mode"],
        footer="由账本确定性统计",
    )


def board_card(rows: list[dict[str, Any]], *, title: str, today: str, limit: int = 10) -> Card:
    risky = [r for r in rows if any(x["level"] == "high" for x in r["risks"])]
    text = "没有在手订单。" if not rows else f"在手订单 {len(rows)} 个，有交期风险 {len(risky)} 个。"
    fields = []
    for p in rows[:limit]:
        q = p["quantities"]
        high = [r["text"] for r in p["risks"] if r["level"] == "high"]
        prod = p["product"]["code"] or p["product"]["name"]
        value = (
            f"{p['customer']['name']} {prod} {p['qty']:g}{p['unit'] or ''}，"
            f"{p['stage_text']}，发货 {q['shipped']:g}，交期 {p['due_date'] or '未填'}"
        )
        if high:
            value = "⚠ " + high[0] + "；" + value
        fields.append(CardField(label=_cut(p["order_no"], 20), value=_cut(value, 200)))
    if len(rows) > limit:
        text += f"（只列前 {limit} 个）"
    mode = "mock" if any(p["data_mode"] == "mock" for p in rows) else "real"
    return Card(title=title, text=text, fields=fields, data_mode=mode, footer=f"{today} 由账本确定性统计")
