"""人力、排班、计件与特种资质 IM 卡片与 Fast Path 关键词（零模型成本）。"""

from __future__ import annotations

import re
from typing import Any

from fos.channels.cards import Card, CardField

ROSTER = re.compile(r"^(?:排班|考勤|排班考勤)$")
CERTS = re.compile(r"^(?:资质|持证|特种资质|证照)$")
PAYROLL = re.compile(r"^(?:工资|计件|薪酬|计件工资)$")


def roster_card(board: dict[str, Any]) -> Card:
    c = board["counts"]
    fields = [
        CardField(
            label=f"{it['work_date']} {it['employee_name']}"[:20],
            value=f"{it['shift_text']}·{it['attendance_text']}·{it['hours']:g}h·计件{it['piece_qty']:g}",
        )
        for it in board["items"][:6]
    ]
    text = f"共 {c['total']} 条：出勤 {c['present']} / 缺勤 {c['absent']} / 计件 {c['total_pieces']:g}。"
    return Card(
        title="班次排班与计件考勤",
        text=text,
        fields=fields,
        data_mode=board["data_mode"],
        footer="详见控制台 /app/hr.html",
    )


def certs_card(board: dict[str, Any]) -> Card:
    c = board["counts"]
    fields = [
        CardField(
            label=f"{it['employee_name']} {it['kind_text']}"[:20],
            value=f"{it['cert_state_text']} · 到期 {it['expires_date']}",
        )
        for it in board["items"][:6]
    ]
    d = board["date"]
    text = f"{d}：共 {c['total']} 项证照，过期 {c['expired']} / 临期 {c['expiring']} / 有效 {c['ok']}。"
    return Card(
        title="特种作业资质预警",
        text=text,
        fields=fields,
        data_mode=board["data_mode"],
        footer="详见控制台 /app/hr.html",
    )


def payroll_card(report: dict[str, Any]) -> Card:
    t = report["totals"]
    fields = [
        CardField(
            label=f"{it['emp_no']} {it['name']}"[:20],
            value=f"出勤 {it['present_days']}天 · 计件 {it['piece_pay']:g} · 应发 {it['gross_pay']:g}元",
        )
        for it in report["items"][:6]
    ]
    m = report["month"]
    text = f"{m} 共 {t['headcount']} 人：应发合计 {t['gross_pay']:g} 元（计件 {t['piece_pay']:g} 元）。"
    return Card(
        title="月度计件薪酬汇总",
        text=text,
        fields=fields,
        data_mode=report["data_mode"],
        footer="详见控制台 /app/hr.html",
    )
