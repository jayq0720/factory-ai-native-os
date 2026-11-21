"""研发配方与试验 IM 卡片与 Fast Path 关键词（零模型成本）。"""

from __future__ import annotations

import re
from typing import Any

from fos.channels.cards import Card, CardField

PROJECTS = re.compile(r"^(?:研发|研发项目|新产品)$")
FORMULAS = re.compile(r"^(?:配方|配方版本|配比)$")
TRIALS = re.compile(r"^(?:试验|小试|中试|试验记录)$")


def projects_card(board: dict[str, Any]) -> Card:
    c = board["counts"]
    fields = [
        CardField(
            label=f"{it['project_no']} {it['name']}"[:20],
            value=f"{it['target_product']} · {it['status_text']} · 结题 {it['due_date']}",
        )
        for it in board["items"][:6]
    ]
    text = f"共 {c['total']} 项：研发中 {c['active']} / 试产 {c['trial']} / 已量产 {c['released']}。"
    return Card(
        title="研发项目台账",
        text=text,
        fields=fields,
        data_mode=board["data_mode"],
        footer="详见控制台 /app/rnd.html",
    )


def formulas_card(board: dict[str, Any]) -> Card:
    c = board["counts"]
    fields = [
        CardField(
            label=f"{it['version_code']} {it['project_name']}"[:20],
            value=f"Al2O3 {it['calc_al2o3_pct']}% · {it['est_cost_per_ton']}元/吨 · {it['status_text']}",
        )
        for it in board["items"][:6]
    ]
    text = f"共 {c['total']} 个配方：试验中 {c['testing']} / 已定型 {c['approved']} / 作废 {c['rejected']}。"
    return Card(
        title="耐火材料配方版本",
        text=text,
        fields=fields,
        data_mode=board["data_mode"],
        footer="详见控制台 /app/rnd.html",
    )


def trials_card(board: dict[str, Any]) -> Card:
    c = board["counts"]
    fields = [
        CardField(
            label=f"{it['trial_no']} {it['formula_code']}"[:20],
            value=f"{it['stage_text']} · {'达标' if it['passed'] else '不达标'} · {it['trial_date']}",
        )
        for it in board["items"][:6]
    ]
    text = f"共 {c['total']} 次试验：达标 {c['passed']} / 不达标 {c['failed']}（合格率 {c['pass_rate']}%）。"
    return Card(
        title="研发试验检测记录",
        text=text,
        fields=fields,
        data_mode=board["data_mode"],
        footer="详见控制台 /app/rnd.html",
    )
