"""SaaS 运维状态与厂端网关车队 IM 卡片与 Fast Path 关键词（零模型成本）。"""

from __future__ import annotations

import re
from typing import Any

from fos.channels.cards import Card, CardField

OPS_STATUS = re.compile(r"^(?:系统状态|运维状态|预算状态|平台状态)$")
EDGE_FLEET = re.compile(r"^(?:网关状态|厂端网关|边缘网关)$")


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
