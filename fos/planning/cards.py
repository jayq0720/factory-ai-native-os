"""IM 卡片（只读 Fast Path，零模型成本）：
- 「排窑」「排窑计划」：当前确认的排窑计划（近几个窑次 + 有风险的订单）；
- 「缺料」：按当前计划算的原料缺口；
- 「交期评估 订单号」「能不能交 订单号」：把这个订单放进去重排一次，看能不能按期、影响谁。
"""

from __future__ import annotations

import re
from typing import Any

from fos.channels.cards import Card, CardField

PLAN = re.compile(r"^(?:@\S+\s+)*(排窑|排窑计划|窑炉排程)$")
SHORTAGE = re.compile(r"^(?:@\S+\s+)*(缺料|物料缺口|缺什么料)$")
EVALUATE = re.compile(
    r"^(?:@\S+\s+)*(?:交期评估|评估交期|能不能交|产能评估)\s*[:：]?\s*([\w\-.]*\d[\w\-.]*)$"
)


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _hm(iso: str) -> str:
    return iso[5:16].replace("T", " ")  # MM-DD HH:MM


def plan_card(p: dict[str, Any] | None, *, limit: int = 8) -> Card:
    if p is None:
        return Card(
            title="排窑计划", text="还没有确认的排窑计划。计划员可以在网页“计划排窑”里生成建议并确认。"
        )
    risky = [o for o in p["orders"] if not o["ok"]]
    text = f"计划 {p['plan']['plan_no']}：{len(p['runs'])} 个窑次，覆盖订单 {len(p['orders'])} 个。"
    if risky:
        text += "\n" + "\n".join(f"- **{o['order_no']}：{o['status_text']}**" for o in risky[:4])
    fields = []
    for r in p["runs"][:limit]:
        loads = "、".join(f"{ld['order_no']} {ld['qty']:g}" for ld in r["loads"])
        fields.append(
            CardField(
                label=_cut(r["run_no"], 20),
                value=_cut(
                    f"{_hm(r['start_at'])}→{_hm(r['end_at'])}，{r['qty']:g}{r['unit'] or ''}：{loads}", 200
                ),
            )
        )
    if len(p["runs"]) > limit:
        text += f"\n（只列前 {limit} 个窑次）"
    return Card(
        title=_cut(f"排窑计划 {p['plan']['plan_no']}", 40),
        text=_cut(text, 500),
        fields=fields,
        data_mode=p["data_mode"],
        footer="求解器建议、计划员确认；数字由代码计算",
    )


def shortage_card(mp: dict[str, Any], *, mode: str, title: str = "原料缺口", limit: int = 10) -> Card:
    rows = mp["shortages"]
    text = "按当前排窑计划，原料都够。" if not rows else f"按当前排窑计划，{len(rows)} 种原料不够。"
    if mp["warnings"]:
        text += "\n" + "\n".join(f"- {w}" for w in mp["warnings"][:3])
    fields = [
        CardField(
            label=_cut(r["name"], 20),
            value=_cut(
                f"缺 {r['shortage']:g} {r['unit']}，{r['short_from']} 起要用"
                f"（需 {r['need']:g}，现存 {r['on_hand']:g}，"
                f"在途 {r['incoming'] + r['on_order']:g}）",
                200,
            ),
        )
        for r in rows[:limit]
    ]
    return Card(
        title=title, text=_cut(text, 500), fields=fields, data_mode=mode, footer=_cut(mp["note"], 100)
    )


def evaluation_card(e: dict[str, Any], *, mode: str) -> Card:
    t = e["target"]
    title = f"交期评估 {t['order_no']}"
    fields = []
    if "qty" in t:
        fields += [
            CardField(label="产品", value=_cut(f"{t['product_code']} {t['qty']:g}{t['unit']}", 200)),
            CardField(label="交期", value=t["due_date"] or "未填"),
            CardField(label="预计可发货", value=t["ready_date"] or "排不下"),
        ]
    for i in e["impacts"][:5]:
        fields.append(
            CardField(
                label=_cut(f"影响 {i['order_no']}", 20),
                value=_cut(
                    f"{i['customer']} 可发货 {i['ready_before'] or '-'} → {i['ready_after'] or '排不下'}"
                    + (f"，交期 {i['due_date']}" if i["due_date"] else ""),
                    200,
                ),
            )
        )
    for s in e["shortages"][:4]:
        fields.append(
            CardField(
                label=_cut(f"缺 {s['name']}", 20),
                value=f"{s['shortage']:g} {s['unit']}，{s['short_from']} 起",
            )
        )
    return Card(
        title=_cut(title, 40),
        text=_cut(("✅ " if e["ok"] else "⚠ ") + e["verdict"], 500),
        fields=fields[:12],
        data_mode=mode,
        footer="把订单放进排窑求解器重排一次的结果，供计划员判断",
    )
