"""IM 卡片与指令（Fast Path，零模型成本）：
- 「窑况」：所有窑炉的在烧窑次、代表温度（含目标温度和偏差）、未关闭告警；
- 「窑况 1号梭式窑」：一个窑炉每个测点的最新读数；
- 「抄表 测点 数值」：路径 C 人工报数（报数人本人输入数值，即本人确认；照片读数走录单卡片确认）。
"""

from __future__ import annotations

import re
from typing import Any

from fos.channels.cards import Card, CardField

BOARD = re.compile(r"^(?:@\S+\s+)*(?:窑况|窑炉状态|看窑)(?:\s*[:：]?\s*(\S.{0,29}))?$")
METER = re.compile(r"^(?:@\S+\s+)*抄表\s*[:：]?\s*(\S+)\s+(-?\d+(?:\.\d+)?)\s*(\S{0,8})$")


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _v(x: float | None) -> str:
    return "—" if x is None else f"{x:.1f}".rstrip("0").rstrip(".")


def _point_line(p: dict[str, Any]) -> str:
    r = p["latest"]
    if r is None:
        return f"{p['source_name']}，还没有读数"
    s = f"{_v(r['value'])}{p['unit']}"
    if p["expected"] is not None:
        s += f"（目标 {_v(p['expected'])}，偏差 {p['deviation']:+.0f}）"
    age = p["age_minutes"]
    s += f"，{_v(age)} 分钟前" if age and age >= 1 else "，刚刚"
    if p["source"] == "manual":
        s += "，抄表"
    if r["quality"] != "good":
        s += "，估读" if r["quality"] == "estimated" else "，质量差"
    return s


def _headline(k: dict[str, Any]) -> str:
    run = k["run"]
    temps = [p for p in k["points"] if p["kind"] == "temperature" and p["latest"]]
    top = max(temps, key=lambda p: p["latest"]["value"], default=None)
    s = f"在烧 {run['run_no']}，第 {_v(run['hours'])} 小时" if run else "未在烧"
    if run and run["curve"]:
        s += f"（{run['curve']}）"
    if top:
        s += f"，{top['name']} {_point_line(top)}"
    if k["alarms"]:
        s += f"，⚠ {len(k['alarms'])} 条告警"
    return s


def board_card(b: dict[str, Any], *, limit: int = 10) -> Card:
    kilns = b["kilns"]
    n_alarm = sum(len(k["alarms"]) for k in kilns) + len(b["alarms"])
    firing = sum(1 for k in kilns if k["run"])
    text = f"{len(kilns)} 座窑炉，{firing} 座在烧，" + (
        f"**{n_alarm} 条未关闭告警**。" if n_alarm else "没有告警。"
    )
    for a in b["alarms"][:3]:
        text += f"\n⚠ {_cut(a['message'], 120)}"
    text += "\n发“窑况 窑炉名”看全部测点。"
    fields = [CardField(label=_cut(k["name"], 20), value=_cut(_headline(k), 200)) for k in kilns[:limit]]
    return Card(
        title="窑况",
        text=_cut(text, 500),
        fields=fields,
        data_mode=b["data_mode"],
        footer="读数来自测点（直连 / 加装采集 / 抄表）；偏差 = 实测 − 烧成制度目标",
    )


def kiln_card(k: dict[str, Any], *, mode: str, limit: int = 10) -> Card:
    text = _headline(k)
    for a in k["alarms"][:3]:
        text += f"\n⚠ {_cut(a['message'], 120)}"
    fields = [
        CardField(label=_cut(p["name"], 20), value=_cut(_point_line(p), 200)) for p in k["points"][:limit]
    ]
    return Card(title=_cut(f"窑况 · {k['name']}", 40), text=_cut(text, 500), fields=fields, data_mode=mode)


def meter_reply(point_name: str, code: str, value: float, unit: str, warn: str | None) -> str:
    s = f"已记录 {point_name}（{code}）：{_v(value)}{unit}。"
    return s + (f"\n⚠ {warn}" if warn else "")
