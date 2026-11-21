"""设备管理（EAM）IM 卡片与 Fast Path 关键词（零模型成本）。"""

from __future__ import annotations

import re
from typing import Any

from fos.channels.cards import Card, CardField

EQUIPMENT = re.compile(r"^(?:设备|设备台账|设备状态)$")
PM_DUE = re.compile(r"^(?:保养|保养计划|待保养)$")
WORK_ORDERS = re.compile(r"^(?:工单|维修工单|报修|故障工单)$")
SPARE_PARTS = re.compile(r"^(?:备件|备件库存|备品备件)$")


def equipment_card(board: dict[str, Any]) -> Card:
    c = board["counts"]
    fields = [
        CardField(
            label=f"{it['code']} {it['name']}"[:20],
            value=f"{it['status_text']} · {it['criticality']}类 · {it.get('location') or '未填位置'}",
        )
        for it in board["items"][:6]
    ]
    return Card(
        title="设备台账状态",
        text=f"共 {c['total']} 台：运行 {c['running']} / 维保 {c['maintenance']} / 闲置 {c['idle']}。",
        fields=fields,
        data_mode=board["data_mode"],
        footer="详见控制台 /app/eam.html",
    )


def pm_card(board: dict[str, Any]) -> Card:
    c = board["counts"]
    fields = [
        CardField(
            label=it["equipment_name"][:20],
            value=f"{it['title']} · {it['pm_text']}（{it['next_due_date']}）",
        )
        for it in board["items"][:6]
    ]
    return Card(
        title="设备保养计划",
        text=f"截至 {board['date']}：超期 {c['overdue']} 项，临期 {c['due_soon']} 项，正常 {c['ok']} 项。",
        fields=fields,
        data_mode=board["data_mode"],
        footer="详见控制台 /app/eam.html",
    )


def work_orders_card(rows: list[dict[str, Any]]) -> Card:
    open_n = sum(1 for r in rows if r["status"] in ("open", "in_progress"))
    fields = [
        CardField(
            label=r["wo_no"][:20],
            value=f"[{r['priority_text']}] {r['equipment_name']} · {r['status_text']} · {r['symptom'][:26]}",
        )
        for r in rows[:6]
    ]
    return Card(
        title="维修工单看板",
        text=f"共 {len(rows)} 张工单，其中未完工 {open_n} 张。",
        fields=fields,
        footer="详见控制台 /app/eam.html",
    )


def spare_parts_card(board: dict[str, Any]) -> Card:
    c = board["counts"]
    fields = [
        CardField(
            label=it["name"][:20],
            value=f"现存 {it['on_hand']:g} {it['unit']}"
            + (f"（安全库存 {it['safety_stock']:g} ⚠）" if it["low_stock"] else ""),
        )
        for it in board["items"][:6]
    ]
    return Card(
        title="备件库存看板",
        text=f"共 {c['total']} 种备件，其中低库存预警 {c['low_stock']} 种。",
        fields=fields,
        data_mode=board["data_mode"],
        footer="备件由仓储模块统一出入库管理",
    )
