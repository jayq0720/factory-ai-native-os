"""筑炉施工与烘炉验收 IM 卡片与 Fast Path 关键词（零模型成本）。"""

from __future__ import annotations

import re
from typing import Any

from fos.channels.cards import Card, CardField

SITES = re.compile(r"^(?:施工|筑炉|施工项目|验收)$")
LOGS = re.compile(r"^(?:施工日报|施工日志|砌筑进度)$")
DRYOUT = re.compile(r"^(?:烘炉|烘炉曲线|升温记录)$")


def sites_card(board: dict[str, Any]) -> Card:
    c = board["counts"]
    fields = [
        CardField(
            label=f"{it['site_no']} {it['name']}"[:20],
            value=f"{it['status_text']} · 进度 {it['progress_pct']:g}% · {it['supervisor']}",
        )
        for it in board["items"][:6]
    ]
    text = f"共 {c['total']} 项：砌筑 {c['building']} / 烘炉 {c['drying']} / 已验收 {c['accepted']}。"
    return Card(
        title="筑炉施工项目看板",
        text=text,
        fields=fields,
        data_mode=board["data_mode"],
        footer="详见控制台 /app/construction.html",
    )


def logs_card(board: dict[str, Any]) -> Card:
    c = board["counts"]
    fields = [
        CardField(
            label=f"{it['log_date']} {it['site_name']}"[:20],
            value=f"{it['crew_size']}人·{it['tons_installed']:g}t·{'安全' if it['safety_ok'] else '异常'}",
        )
        for it in board["items"][:6]
    ]
    text = f"共 {c['total']} 份施工日报，累计砌筑 {c['total_tons']:g} 吨。"
    return Card(
        title="筑炉施工日报",
        text=text,
        fields=fields,
        data_mode=board["data_mode"],
        footer="详见控制台 /app/construction.html",
    )


def dryout_card(board: dict[str, Any]) -> Card:
    c = board["counts"]
    fields = [
        CardField(
            label=f"{it['site_name']} {it['stage_name']}"[:20],
            value=f"{it['actual_temp_c']:g}/{it['target_temp_c']:g}℃ · {it['ramp_rate_c_per_h']:g}℃/h",
        )
        for it in board["items"][:6]
    ]
    text = f"共 {c['total']} 条烘炉测温：正常 {c['within_spec']} / 告警 {c['alerts']}。"
    return Card(
        title="烘炉升温监测看板",
        text=text,
        fields=fields,
        data_mode=board["data_mode"],
        footer="详见控制台 /app/construction.html",
    )
