"""IM 卡片（只读 Fast Path，零模型成本）：
- 「库存」「库存预警」：低于安全库存的物料；
- 「库存 高铝矾土」：一种物料的可用、待检、不可用和批次（先进先出顺序）；
- 「请购建议」：按排窑计划缺口和安全库存算的请购建议（只是建议，到网页确认）。
"""

from __future__ import annotations

import re
from typing import Any

from fos.channels.cards import Card, CardField

STOCK = re.compile(r"^(?:@\S+\s+)*(?:库存|查库存|现存)(?:\s*[:：]?\s*(.+))?$")
SUGGEST = re.compile(r"^(?:@\S+\s+)*(请购建议|要买什么|采购建议)$")


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _qty(r: dict[str, Any]) -> str:
    s = f"可用 {r['usable']:g} {r['unit']}"
    if r["incoming"]:
        s += f"，待检 {r['incoming']:g}"
    if r["blocked"]:
        s += f"，不可用 {r['blocked']:g}"
    if r["safety_stock"]:
        s += f"，安全库存 {r['safety_stock']:g}"
    return s


def low_stock_card(rows: list[dict[str, Any]], *, mode: str, limit: int = 10) -> Card:
    low = [r for r in rows if r["below_safety"]]
    text = (
        f"{len(low)} 种物料低于安全库存。" if low else f"{len(rows)} 种物料，没有低于安全库存的。"
    ) + "\n发“库存 物料名”看批次明细。"
    fields = [CardField(label=_cut(r["name"], 20), value=_cut(_qty(r), 200)) for r in low[:limit]]
    return Card(
        title="库存预警",
        text=text,
        fields=fields,
        data_mode=mode,
        footer="实时库存 = 到货 − 领用 + 退料 ± 盘点",
    )


def material_card(r: dict[str, Any], *, mode: str, limit: int = 8) -> Card:
    text = ("⚠ 低于安全库存。" if r["below_safety"] else "") + _qty(r)
    fields = [
        CardField(
            label=_cut(lot["lot"], 20),
            value=_cut(
                f"{lot['status_text']} {lot['on_hand']:g} {lot['unit']}"
                + (f"，库位 {lot['location']}" if lot["location"] else "")
                + (f"，{str(lot['received_at'])[:10]} 到货" if lot["received_at"] else ""),
                200,
            ),
        )
        for lot in r["lots"][:limit]
    ]
    if len(r["lots"]) > limit:
        text += f"\n（只列前 {limit} 个批次）"
    return Card(
        title=_cut(f"库存 {r['name']}", 40),
        text=_cut(text, 500),
        fields=fields,
        data_mode=mode,
        footer="批次按先进先出排列；待检、不合格的批次不能领",
    )


def suggestion_card(s: dict[str, Any], *, mode: str, limit: int = 10) -> Card:
    rows = s["suggestions"]
    text = (
        f"{len(rows)} 种物料建议请购。"
        if rows
        else "现在没有要请购的：排窑计划的原料够、也没有低于安全库存。"
    )
    if rows:
        text += "\n在网页“采购仓储”核对后生成请购单。"
    fields = [
        CardField(
            label=_cut(r["name"], 20),
            value=_cut(
                f"请购 {r['qty']:g} {r['unit']}"
                + (f"，{r['need_date']} 前要" if r["need_date"] else "")
                + f"：{r['reason']}",
                200,
            ),
        )
        for r in rows[:limit]
    ]
    return Card(
        title="请购建议",
        text=_cut(text, 500),
        fields=fields,
        data_mode=mode,
        footer="只是建议，采购员确认后才入账",
    )
