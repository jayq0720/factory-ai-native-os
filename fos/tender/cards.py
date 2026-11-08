"""IM 卡片（只读 Fast Path，零模型成本）：
- 「招标」「投标」：进行中的招标项目，按投标截止时间排序，带★条款未处理数；
- 「招标 编号」：一个招标项目的关键信息、拆解进度和风险；
- 截止提醒（fos/tender/notify.py 推送）。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from fos.channels.cards import Card, CardField

TENDER = re.compile(r"^(?:@\S+\s+)*(?:招标|投标|招标项目)(?:\s*[:：]?\s*(\S{2,40}))?$")


def _left(hours: float | None) -> str:
    if hours is None:
        return ""
    if hours <= 0:
        return "已截止"
    return f"还有 {hours / 24:.1f} 天" if hours >= 48 else f"还有 {hours:.0f} 小时"


def _deadline(t: dict[str, Any]) -> str:
    d = t.get("bid_deadline")
    return datetime.fromisoformat(d).strftime("%m-%d %H:%M") if d else "-"


def list_card(items: list[dict[str, Any]], *, mode: str, limit: int = 10) -> Card:
    fields = []
    for t in items[:limit]:
        s = (t.get("analysis") or {}).get("summary") or {}
        extra = f"，★ 未处理 {s['star_open']}" if s.get("star_open") else ""
        extra += f"，缺资料 {s['docs_missing']}" if s.get("docs_missing") else ""
        fields.append(
            CardField(
                label=f"{t['id']}",
                value=f"{t.get('name', '')[:30]}｜截止 {_deadline(t)}（{_left(t.get('hours_left'))}）{extra}"[
                    :200
                ],
            )
        )
    return Card(
        title="进行中的招标项目",
        text=(f"{len(items)} 个招标项目在准备投标。" if items else "没有进行中的招标项目。")
        + "\n发“招标 编号”看详情。",
        fields=fields,
        data_mode=mode,
        footer="截止时间来自招标文件，已人工核对",
    )


def tender_card(
    t: dict[str, Any], analysis: dict[str, Any] | None, risks: list[dict[str, Any]], *, mode: str
) -> Card:
    fields = [
        CardField(label="招标编号", value=t.get("tender_no") or "-"),
        CardField(label="投标截止", value=f"{_deadline(t)}（{_left(t.get('hours_left'))}）"),
    ]
    if t.get("budget"):
        fields.append(CardField(label="最高限价", value=f"{t['budget'] / 10000:g} 万元"))
    if t.get("purchaser"):
        fields.append(CardField(label="招标人", value=t["purchaser"][:60]))
    if analysis:
        s = analysis["summary"]
        fields.append(
            CardField(
                label="需求矩阵",
                value=f"{s['total']} 条，★ {s['star']} 条（未处理 {s['star_open']}），"
                f"负偏离 {s['negative']}，资料缺 {s['docs_missing']}/{s['docs_total']}",
            )
        )
    for r in risks[:5]:
        fields.append(
            CardField(
                label=r["type"], value=(f"第 {r['page']} 页 " if r.get("page") else "") + r["text"][:160]
            )
        )
    return Card(
        title=f"招标：{t.get('name', '')[:30]}",
        text=f"状态：{t.get('status_name') or t.get('status')}",
        fields=fields,
        data_mode=mode,
        footer="需求矩阵、偏离表、资料清单在网页 /app/tender.html 处理",
    )


def reminder_text(
    t: dict[str, Any], label: str, due: datetime, hours: float, summary: dict[str, Any] | None
) -> str:
    no = t.get("tender_no") or t["id"]
    s = f"⏰ {label}还有 {hours:.0f} 小时：{t.get('name', '')}（{no}），{due:%m-%d %H:%M} 截止。"
    if summary:
        if summary["star_open"]:
            s += f"★条款还有 {summary['star_open']} 条没处理。"
        if summary["docs_missing"]:
            s += f"资料缺 {summary['docs_missing']} 项。"
    return ("【模拟】" if t.get("data_mode") == "mock" else "") + s
