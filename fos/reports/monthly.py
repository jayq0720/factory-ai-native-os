"""全厂月度经营与合规汇总报表引擎：跨模块确定性汇总并输出 Markdown / 正式三表 Excel。"""

from __future__ import annotations

import io
import re
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from openpyxl import Workbook
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import Approval, Event, Run, Todo
from fos.excel_safe import excel_row
from fos.market.service import executive_cockpit

if TYPE_CHECKING:
    from fos.services import Services

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _month_bounds(month: str) -> tuple[datetime, datetime, date]:
    if not MONTH_RE.match(month):
        raise ValueError(f"月份格式需为 YYYY-MM：{month}")
    year, mon = int(month[:4]), int(month[5:7])
    start = datetime(year, mon, 1, tzinfo=UTC)
    if mon == 12:
        end = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(year, mon + 1, 1, tzinfo=UTC)
    return start, end, date(year, mon, 1)


async def build_monthly_report(
    session: AsyncSession,
    svc: Services,
    tenant_id: str,
    month: str,
) -> dict[str, Any]:
    """跨模块确定性生成月度经营与治理报表（规则 4：正式月报严格仅汇总真实数据）。"""
    tenant = svc.registry.get(tenant_id)
    if tenant is None:
        raise ValueError(f"租户 {tenant_id} 不存在")
    start, end, as_of = _month_bounds(month)

    cockpit = await executive_cockpit(session, tenant_id, as_of, mode="real")

    ev_rows = (
        await session.execute(
            select(Event.type, func.count())
            .where(
                Event.tenant_id == tenant_id,
                Event.data_mode == "real",
                Event.occurred_at >= start,
                Event.occurred_at < end,
            )
            .group_by(Event.type)
        )
    ).all()
    domain_counts: dict[str, int] = {}
    events_month = 0
    for ev_type, cnt in ev_rows:
        c = int(cnt or 0)
        events_month += c
        dom = str(ev_type).split(".", 1)[0]
        domain_counts[dom] = domain_counts.get(dom, 0) + c

    todo_rows = (
        await session.execute(
            select(Todo.status, func.count())
            .where(Todo.tenant_id == tenant_id, Todo.created_at >= start, Todo.created_at < end)
            .group_by(Todo.status)
        )
    ).all()
    todo_by_status = {str(st): int(cnt or 0) for st, cnt in todo_rows}
    todos_total = sum(todo_by_status.values())
    todos_done = todo_by_status.get("done", 0)
    todo_close_rate = round(todos_done / todos_total * 100.0, 1) if todos_total else 100.0

    app_rows = (
        await session.execute(
            select(Approval.status, func.count())
            .where(
                Approval.tenant_id == tenant_id,
                Approval.created_at >= start,
                Approval.created_at < end,
            )
            .group_by(Approval.status)
        )
    ).all()
    app_by_status = {str(st): int(cnt or 0) for st, cnt in app_rows}

    tok_row = (
        await session.execute(
            select(
                func.coalesce(func.sum(Run.prompt_tokens), 0),
                func.coalesce(func.sum(Run.completion_tokens), 0),
            ).where(Run.tenant_id == tenant_id, Run.created_at >= start, Run.created_at < end)
        )
    ).one()
    llm_tokens = int(tok_row[0] or 0) + int(tok_row[1] or 0)

    kpis = cockpit["kpis"]
    risks: list[dict[str, Any]] = []
    if kpis.get("open_hazards", 0) > 0:
        risks.append({"domain": "EHS 安全环保", "item": "未闭环安全隐患", "count": kpis["open_hazards"]})
    if kpis.get("open_ncrs", 0) > 0:
        risks.append({"domain": "质量体系", "item": "未关闭不合格品 NCR", "count": kpis["open_ncrs"]})
    if kpis.get("open_work_orders", 0) > 0:
        risks.append({"domain": "设备管理", "item": "未闭环维修工单", "count": kpis["open_work_orders"]})
    if app_by_status.get("expired", 0) > 0:
        risks.append({"domain": "审批治理", "item": "本月超时过期审批", "count": app_by_status["expired"]})

    data: dict[str, Any] = {
        "tenant_id": tenant_id,
        "factory_name": tenant.name,
        "month": month,
        "data_mode": "real",
        "kpis": kpis,
        "funnel": cockpit["funnel"],
        "activity": {
            "events_total": events_month,
            "domain_counts": dict(sorted(domain_counts.items())),
            "todos_total": todos_total,
            "todos_done": todos_done,
            "todo_close_rate_pct": todo_close_rate,
            "approvals": app_by_status,
            "llm_tokens": llm_tokens,
        },
        "risks": risks,
    }
    data["markdown"] = render_monthly_markdown(data)
    return data


def render_monthly_markdown(report: dict[str, Any]) -> str:
    """将月度经营数据渲染为适合 IM 推送与管理层审阅的中文 Markdown 报告。"""
    k = report["kpis"]
    act = report["activity"]
    lines = [
        f"# {report['factory_name']} · {report['month']} 经营与合规月报",
        "",
        "## 一、核心经营指标（仅统计真实账本数据）",
        f"- 财务往来：应收余额 ¥{k.get('ar_balance', 0):,.2f} / 应付余额 ¥{k.get('ap_balance', 0):,.2f}",
        (
            f"- 市场商机：在途加权额 ¥{k.get('weighted_pipeline_amount', 0):,.2f} / "
            f"赢单额 ¥{k.get('won_amount', 0):,.2f}"
        ),
        (
            f"- 生产保障：在册员工 {k.get('active_employees', 0)} 人 | "
            f"未闭环维修工单 {k.get('open_work_orders', 0)} 单"
        ),
        (
            f"- 质量与安全：未关闭 NCR {k.get('open_ncrs', 0)} 项 | "
            f"未闭环安全隐患 {k.get('open_hazards', 0)} 项"
        ),
        (
            f"- 研发与工程：在研项目 {k.get('active_rnd_projects', 0)} 项 | "
            f"在建筑炉项目 {k.get('active_sites', 0)} 项"
        ),
        "",
        "## 二、月度系统运行与治理",
        f"- 本月账本事件：{act['events_total']} 条（Token 消耗 {act['llm_tokens']:,}）",
        f"- 本月待办闭环：{act['todos_done']}/{act['todos_total']}（闭环率 {act['todo_close_rate_pct']}%）",
    ]
    if act["domain_counts"]:
        dom_str = "、".join(f"{d}:{c}" for d, c in act["domain_counts"].items())
        lines.append(f"- 分域事件分布：{dom_str}")
    lines.append("")
    lines.append("## 三、重点风险与整改项")
    if not report["risks"]:
        lines.append("- 本月无未闭环安全/质量/设备/审批红线风险项。")
    else:
        for r in report["risks"]:
            lines.append(f"- [{r['domain']}] {r['item']}：{r['count']} 项")
    return "\n".join(lines) + "\n"


def render_monthly_xlsx(report: dict[str, Any]) -> bytes:
    """导出正式三表月度经营 Excel（全部单元格经 excel_row 防公式注入清洗）。"""
    wb = Workbook()

    ws1 = wb.active
    assert ws1 is not None
    ws1.title = "经营总览"
    ws1.append(excel_row(["指标名称", "数值", "备注"]))
    k = report["kpis"]
    act = report["activity"]
    overview_rows = [
        ("工厂名称", report["factory_name"], report["tenant_id"]),
        ("统计月份", report["month"], "仅含真实数据（real）"),
        ("应收余额（元）", k.get("ar_balance", 0.0), "财务应收账款"),
        ("应付余额（元）", k.get("ap_balance", 0.0), "财务应付账款"),
        ("在途商机加权额（元）", k.get("weighted_pipeline_amount", 0.0), "市场商机漏斗"),
        ("累计赢单额（元）", k.get("won_amount", 0.0), "市场商机漏斗"),
        ("未关闭不合格品 NCR", k.get("open_ncrs", 0), "质量体系"),
        ("未闭环维修工单", k.get("open_work_orders", 0), "设备管理 EAM"),
        ("未闭环安全隐患", k.get("open_hazards", 0), "安全环保 EHS"),
        ("在册员工数", k.get("active_employees", 0), "人力薪酬"),
        ("在研项目数", k.get("active_rnd_projects", 0), "研发配方"),
        ("在建筑炉项目数", k.get("active_sites", 0), "筑炉施工"),
        ("本月账本事件数", act["events_total"], "真实业务事件"),
        ("本月待办闭环率（%）", act["todo_close_rate_pct"], f"{act['todos_done']}/{act['todos_total']}"),
        ("本月 LLM Token 消耗", act["llm_tokens"], "AI 运行成本"),
    ]
    for row in overview_rows:
        ws1.append(excel_row(row))

    ws2 = wb.create_sheet("分域事件统计")
    ws2.append(excel_row(["业务域", "本月事件数"]))
    for dom, cnt in act["domain_counts"].items():
        ws2.append(excel_row([dom, cnt]))

    ws3 = wb.create_sheet("风险与整改项")
    ws3.append(excel_row(["业务域", "风险项", "待处理数量"]))
    for r in report["risks"]:
        ws3.append(excel_row([r["domain"], r["item"], r["count"]]))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
