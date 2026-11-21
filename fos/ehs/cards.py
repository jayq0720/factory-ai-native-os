"""EHS 安全环保 IM 卡片与 Fast Path 关键词（零模型成本）。"""

from __future__ import annotations

import re
from typing import Any

from fos.channels.cards import Card, CardField

HAZARDS = re.compile(r"^(?:隐患|安全隐患|隐患台账|安全)$")
PERMITS = re.compile(r"^(?:作业票|特种作业|动火票)$")
ENV = re.compile(r"^(?:环保|环保监测|排放)$")


def hazards_card(board: dict[str, Any]) -> Card:
    c = board["counts"]
    fields = [
        CardField(
            label=f"{it['hazard_no']} {it['location']}"[:20],
            value=f"[{it['level_text']}] {it['status_text']} · 限期 {it['due_date']}"
            + (" ⚠超期" if it["overdue"] else ""),
        )
        for it in board["items"][:6]
    ]
    return Card(
        title="安全隐患排查台账",
        text=f"截至 {board['date']}：共 {c['total']} 项，待改 {c['open']} / 整改 {c['rectifying']} / 超期 {c['overdue']}。",
        fields=fields,
        data_mode=board["data_mode"],
        footer="详见控制台 /app/ehs.html",
    )


def permits_card(board: dict[str, Any]) -> Card:
    c = board["counts"]
    fields = [
        CardField(
            label=f"{it['permit_no']} {it['kind_text']}"[:20],
            value=f"{it['location']} · {it['worker_name']} · {it['status_text']}",
        )
        for it in board["items"][:6]
    ]
    return Card(
        title="特种作业票看板",
        text=f"共 {c['total']} 张作业票：作业中 {c['active']} 张，高风险 {c['high_risk']} 张。",
        fields=fields,
        data_mode=board["data_mode"],
        footer="详见控制台 /app/ehs.html",
    )


def env_card(board: dict[str, Any]) -> Card:
    c = board["counts"]
    fields = [
        CardField(
            label=f"{it['point_name']} {it['kind_text']}"[:20],
            value=f"{it['value']}/{it['limit_value']} {it['unit']} · {'达标' if it['passed'] else '超标 ⚠'}",
        )
        for it in board["items"][:6]
    ]
    return Card(
        title="环保排放监测看板",
        text=f"共 {c['total']} 条监测：达标 {c['passed']} / 超标 {c['exceeded']}（达标率 {c['compliance_rate_pct']}%）。",
        fields=fields,
        data_mode=board["data_mode"],
        footer="详见控制台 /app/ehs.html",
    )
