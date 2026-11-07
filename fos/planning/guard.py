"""计划装窑的账本守卫：pmc.run_planned 写入前逐行检查（跨对象）。

任何入口（接口、/v1/events、审批通过后）都绕不过去：

- 窑炉能排产（不在维修 / 停用），设置了装量和单位，单位和订单一致；
- 产品烧成温度不超过窑炉最高温度；
- 订单未关闭，产品和订单一致；
- 同一版计划里：同一窑次（窑炉 + 窑次号）时间一致、烧成组一致、合计不超过每窑装量；不同窑次在同一窑上不重叠。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from fos.governance.principal import Principal
from fos.ontology import Ontology
from fos.orders.config import OrdersConfig
from fos.orders.data import get, num, referrers
from fos.orders.progress import CLOSED
from fos.planning.demand import UNAVAILABLE

RUN_EVENT = "pmc.run_planned"


def _group(product) -> str:
    return str(product.state.get("firing_group") or f"product:{product.object_id}") if product else ""


async def check_run(session: AsyncSession, tenant: str, ocfg: OrdersConfig, p: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    start, end = datetime.fromisoformat(p["start_at"]), datetime.fromisoformat(p["end_at"])
    if end <= start:
        problems.append("出窑时间要晚于装窑时间")
    plan = await get(session, tenant, "kiln_plan", p["plan"])
    if plan is not None and plan.state.get("status") != "confirmed":
        problems.append("排窑计划已被替代，不能再加装窑行")
    kiln = await get(session, tenant, "kiln", p["kiln"])
    product = await get(session, tenant, "product", p["product"])
    if kiln is None or product is None:
        return problems  # 引用检查已经报过
    ks = kiln.state
    name = ks.get("name") or kiln.object_id
    if ks.get("plan_status") in UNAVAILABLE:
        problems.append(f"窑炉 {name} 在维修或停用")
    cap = num(ks.get("capacity"))
    if cap <= 0 or not ks.get("capacity_unit"):
        problems.append(f"窑炉 {name} 没有设置每窑装量")
    elif not ocfg.same_unit(ks.get("capacity_unit"), p["unit"]):
        problems.append(f"装窑单位 {p['unit']} 和窑炉装量单位 {ks.get('capacity_unit')} 不同")
    ft, mt = product.state.get("firing_temp"), ks.get("max_temp")
    if ft and mt and num(ft) > num(mt):
        problems.append(f"产品烧成温度 {num(ft):g}℃ 超过窑炉 {name} 最高温度 {num(mt):g}℃")
    if p.get("order"):
        order = await get(session, tenant, "sales_order", p["order"])
        if order is not None:
            if order.state.get("status") in CLOSED:
                problems.append(f"订单 {order.state.get('order_no') or order.object_id} 已关闭或取消")
            if order.state.get("product") != p["product"]:
                problems.append("装窑产品和订单产品不一致")
            if not ocfg.same_unit(order.state.get("unit"), p["unit"]):
                problems.append("装窑单位和订单单位不同")

    total = num(p.get("qty"))
    for r in await referrers(session, tenant, "planned_run", "plan", "kiln_plan", [p["plan"]]):
        rs = r.state
        if rs.get("kiln") != p["kiln"]:
            continue
        rstart, rend = datetime.fromisoformat(rs["start_at"]), datetime.fromisoformat(rs["end_at"])
        if rs.get("run_no") == p["run_no"]:
            if (rstart, rend) != (start, end):
                problems.append(f"窑次 {p['run_no']} 的时间和同窑次其他装窑行不一致")
            other = await get(session, tenant, "product", rs.get("product"))
            if _group(other) != _group(product):
                problems.append(f"窑次 {p['run_no']} 里的产品不在同一烧成组，不能同窑装烧")
            total += num(rs.get("qty"))
        elif rstart < end and start < rend:
            problems.append(f"窑次 {p['run_no']} 和 {rs.get('run_no')} 在窑炉 {name} 上时间重叠")
    if cap > 0 and total > cap + 1e-6:
        problems.append(
            f"窑次 {p['run_no']} 装窑合计 {total:g}，超过每窑装量 {cap:g} {ks.get('capacity_unit')}"
        )
    return list(dict.fromkeys(problems))


class PlannedRunGuard:
    def __init__(self, configs: dict[str, OrdersConfig]):
        self.configs = configs

    async def __call__(
        self,
        session: AsyncSession,
        principal: Principal,
        onto: Ontology,
        payload: dict[str, Any],
        state: dict[str, Any] | None,
    ) -> list[str]:
        cfg = self.configs.get(principal.tenant_id)
        if cfg is None:
            return []
        return await check_run(session, principal.tenant_id, cfg, payload)
