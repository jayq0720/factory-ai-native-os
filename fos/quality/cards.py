"""质量体系 IM Fast Path 规则与卡片渲染（只读，零模型成本）。"""

from __future__ import annotations

import re
from typing import Any

from fos.channels.cards import Card, CardField

PENDING = re.compile(r"^(?:@\S+\s+)*(?:质检|待检|待检任务)$")
NCR_LIST = re.compile(r"^(?:@\S+\s+)*(?:不合格|不合格品|NCR|ncr)$")
CALIBRATION = re.compile(r"^(?:@\S+\s+)*(?:校准|计量|计量器具)$")
COA = re.compile(r"^(?:@\S+\s+)*质保书\s*[:：]?\s*([\w\-.]+)$")


def pending_card(data: dict[str, Any]) -> Card:
    inc, fin = data["incoming"], data["final"]
    fields = [
        CardField(label="待检来料", value=f"{len(inc)} 批"),
        CardField(label="待出厂检", value=f"{len(fin)} 批"),
    ]
    lines: list[str] = []
    for x in inc[:4]:
        lines.append(f"- [来料] {x['object_id']} {x['name']}（{x['qty']} {x['unit'] or ''}）")
    for x in fin[:4]:
        lines.append(f"- [出厂] {x['object_id']} {x['name']}（{x['qty']} {x['unit'] or ''}）")
    text = "\n".join(lines) if lines else "当前没有待检任务。"
    return Card(title="待检任务", text=text[:500], fields=fields, data_mode=data["data_mode"])


def ncr_card(rows: list[dict[str, Any]], *, mode: str = "real") -> Card:
    open_rows = [r for r in rows if r["status"] != "closed"]
    lines = [
        f"- {r['ncr_no']} [{r['severity_text']}] {r['status_text']} · {r['summary'][:36]}"
        for r in open_rows[:8]
    ]
    text = "\n".join(lines) if lines else "当前没有未关闭的不合格品报告。"
    return Card(
        title=f"不合格品报告（未关闭 {len(open_rows)}）",
        text=text[:500],
        fields=[CardField(label="总计", value=f"{len(rows)} 条")],
        data_mode=mode,
    )


def calibration_card(board: dict[str, Any]) -> Card:
    c = board["counts"]
    bad = [x for x in board["items"] if x["cal_state"] in ("overdue", "due_soon")]
    lines = [f"- {x['code']} {x['name']}：{x['cal_text']}（{x['next_due_date']}）" for x in bad[:8]]
    text = "\n".join(lines) if lines else "所有在用计量器具均在校准有效期内。"
    return Card(
        title="计量器具校准状态",
        text=text[:500],
        fields=[
            CardField(label="已超期", value=str(c["overdue"])),
            CardField(label="即将到期", value=str(c["due_soon"])),
            CardField(label="正常", value=str(c["ok"])),
        ],
        data_mode=board["data_mode"],
    )


def coa_card(coa: dict[str, Any]) -> Card:
    fields = [
        CardField(label="产品", value=f"{coa['product_code']} {coa['product_name']}".strip() or "—"),
        CardField(label="报告号", value=coa["report_no"]),
        CardField(label="结论", value=coa["result"]),
    ]
    lines = [f"- {c['name']}：{c['value']:g} {c['unit']}（{c['note']}）" for c in coa["checks"][:8]]
    text = "\n".join(lines) if lines else "已通过出厂检验，可在控制台质量页导出 Word / Excel 质保书。"
    return Card(
        title=f"质保书 {coa['batch_id']}",
        text=text[:500],
        fields=fields,
        data_mode=coa["data_mode"],
        footer="数据取自事件账本，可在 /app/quality.html 导出正式文件",
    )
