"""SaaS 运维状态与厂端网关车队 IM 卡片与 Fast Path 关键词（零模型成本）。"""

from __future__ import annotations

import re
from typing import Any

from fos.channels.cards import Card, CardField

OPS_STATUS = re.compile(r"^(?:系统状态|运维状态|预算状态|平台状态)$")
EDGE_FLEET = re.compile(r"^(?:网关状态|厂端网关|边缘网关)$")
LEDGER_CHECK = re.compile(r"^(?:账本校验|账本审计|账本完整性|账本自检)$")


def ops_status_card(
    tenant_name: str,
    budget: dict[str, Any],
    fleet: dict[str, Any],
    *,
    active_users: int,
    bound_users: int,
) -> Card:
    cov = round(bound_users / active_users * 100.0, 1) if active_users else 0.0
    rows = [
        ("Token 预算", f"{budget['status']}（{budget['reason']}）"),
        ("今日消耗", f"{budget['daily_used']} / {budget['daily_limit']}（{budget['daily_pct']}%）"),
        ("本月消耗", f"{budget['monthly_used']} / {budget['monthly_limit']}（{budget['monthly_pct']}%）"),
        ("厂端网关", f"在线 {fleet['gateways_online']} / {fleet['gateways_total']} 台"),
        ("窑炉告警", f"未闭环 {fleet['open_alarms_total']} 条"),
        ("IM 覆盖", f"{bound_users} / {active_users} 活跃员工（{cov}%）"),
    ]
    return Card(
        title=f"{tenant_name} · 系统与运维状态",
        text="零模型成本实时汇总（即使 Token 预算熔断仍可用）。",
        fields=[CardField(label=lb, value=val) for lb, val in rows],
        data_mode="real",
        footer="详见运营总控 /app/ops.html",
    )


def edge_fleet_card(tenant_name: str, fleet: dict[str, Any]) -> Card:
    gws = fleet.get("gateways") or []
    fields = [
        CardField(
            label=str(g["edge_id"])[:20],
            value=(
                f"{'在线' if g['online'] else '离线'} · 延迟 {g['age_s']}s · "
                f"待执行 {g['pending_commands']} · 告警 {g['open_alarms']}"
            ),
        )
        for g in gws[:8]
    ]
    if not fields:
        fields = [CardField(label="网关列表", value="暂无已上报心跳的厂端网关")]
    return Card(
        title=f"{tenant_name} · 厂端网关车队状态",
        text=(
            f"在线 {fleet['gateways_online']}/{fleet['gateways_total']} 台，"
            f"未闭环窑炉告警 {fleet['open_alarms_total']} 条。"
        ),
        fields=fields,
        data_mode="real",
        footer="详见运营总控 /app/ops.html",
    )


def ledger_audit_card(tenant_name: str, report: dict[str, Any]) -> Card:
    seq_txt = "连续无断层" if report["seq_monotonic_ok"] else f"断层 {report['gaps_count']} 处"
    ctr_txt = (
        f"对齐（{report['counter_seq']}）"
        if report["counter_aligned"]
        else f"漂移（counter={report['counter_seq']} != {report['last_seq']}）"
    )
    rows = [
        ("审计状态", "完整通过" if report["ok"] else "发现异常"),
        ("账本事件", f"{report['events_total']} 条（last_seq={report['last_seq']}）"),
        ("序号连续", seq_txt),
        ("计数器对齐", ctr_txt),
        (
            "证据链覆盖",
            f"{report['evidence_coverage_pct']}%（"
            f"{report['real_events_with_evidence']}/{report['real_events_total']}）",
        ),
        ("投影同步", f"{report['projected_objects_count']} / {report['event_distinct_objects_count']} 个对象"),
    ]
    return Card(
        title=f"{tenant_name} · 账本一致性审计",
        text="零模型成本只读实时审计（序号连续性 / 计数器对齐 / 证据链 / 投影同步）。",
        fields=[CardField(label=lb, value=val) for lb, val in rows],
        data_mode="real",
        footer="详见运营总控 /app/ops.html",
    )
