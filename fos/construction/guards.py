"""筑炉施工与烘炉验收账本守卫：注册在 Ledger.add_guard()，任何入口都绕不过去。

- 施工立项：计划砌筑量必须大于 0，计划竣工日不得早于开工日；
- 施工日报：作业人数须 ≥ 1，完成吨位非负有限；
- 竣工验收：已验收项目不能重复验收；正常验收进度须 ≥ min_progress_for_accept_pct（未达标须走带尾项审批）。
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from fos.construction.config import ConstructionConfig
from fos.governance.principal import Principal
from fos.ontology import Ontology

SITE_CREATE_EVENT = "construction.site_created"
SITE_UPDATE_EVENT = "construction.site_updated"
SITE_ACCEPT_EVENT = "construction.site_accepted"
LOG_RECORD_EVENT = "construction.log_recorded"
DRYOUT_LOG_EVENT = "construction.dryout_logged"


def _parse_date(raw: Any) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


class ConstructionGuard:
    """筑炉施工与验收账本不变量守卫。"""

    def __init__(self, event_type: str, configs: dict[str, ConstructionConfig]):
        self.event_type = event_type
        self.configs = configs

    async def __call__(
        self,
        session: AsyncSession,
        principal: Principal,
        onto: Ontology,
        payload: dict[str, Any],
        state: dict[str, Any] | None,
        *,
        object_id: str = "",
        data_mode: str = "real",
    ) -> list[str]:
        cfg = self.configs.get(principal.tenant_id)
        if self.event_type == SITE_CREATE_EVENT:
            problems: list[str] = []
            try:
                tons = float(payload.get("planned_tons"))  # type: ignore[arg-type]
                if not math.isfinite(tons) or tons <= 0:
                    problems.append("计划砌筑量（planned_tons）必须大于 0")
            except (TypeError, ValueError):
                problems.append("计划砌筑量不是有效数字")
            d1, d2 = _parse_date(payload.get("start_date")), _parse_date(payload.get("due_date"))
            if d1 is None or d2 is None:
                problems.append("开竣工日期格式非法")
            elif d2 < d1:
                problems.append("计划竣工日期不能早于开工日期")
            return problems

        if self.event_type == LOG_RECORD_EVENT:
            problems = []
            try:
                crew = int(payload.get("crew_size"))  # type: ignore[arg-type]
                if crew < 1:
                    problems.append("作业人数（crew_size）必须至少为 1")
            except (TypeError, ValueError):
                problems.append("作业人数不是有效整数")
            try:
                tons = float(payload.get("tons_installed"))  # type: ignore[arg-type]
                if not math.isfinite(tons) or tons < 0:
                    problems.append("当日完成量必须为非负有限数字")
            except (TypeError, ValueError):
                problems.append("当日完成量不是有效数字")
            return problems

        if self.event_type == SITE_ACCEPT_EVENT:
            if state is None:
                return ["施工项目不存在"]
            if state.get("status") == "accepted":
                return ["施工项目已竣工验收，不能重复验收"]
            if payload.get("status") != "accepted":
                return ["竣工验收状态必须为 accepted"]
            acc_type = payload.get("acceptance_type") or "normal"
            min_pct = cfg.min_progress_for_accept_pct if cfg is not None else 95.0
            cur_pct = float(state.get("progress_pct") or 0.0)
            if acc_type == "normal" and cur_pct < min_pct:
                return [
                    f"当前砌筑进度 {cur_pct:g}% 低于正常验收门槛 {min_pct:g}%，"
                    "请补齐施工进度或改走带尾项条件验收（conditional）"
                ]
            return []

        return []
