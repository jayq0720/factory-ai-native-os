"""设备管理（EAM）账本守卫：注册在 Ledger.add_guard()，任何入口都绕不过去。

- 保养完成：下次保养到期日必须晚于本次完成日期；
- 维修工单：禁止重复开工/完工，完工必须填写处理措施且停机时长非负有限；
- 设备报废：已报废设备不能重复报废。
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from fos.governance.principal import Principal
from fos.ontology import Ontology

EQUIP_REG_EVENT = "eam.equipment_registered"
EQUIP_UPD_EVENT = "eam.equipment_updated"
EQUIP_RETIRE_EVENT = "eam.equipment_retired"
PM_CREATE_EVENT = "eam.pm_plan_created"
PM_DONE_EVENT = "eam.pm_completed"
WO_OPEN_EVENT = "eam.work_order_opened"
WO_START_EVENT = "eam.work_order_started"
WO_DONE_EVENT = "eam.work_order_completed"
INSP_LOG_EVENT = "eam.inspection_logged"


class EamGuard:
    """EAM 账本不变量守卫。"""

    def __init__(self, event_type: str):
        self.event_type = event_type

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
        if self.event_type == PM_DONE_EVENT:
            if state is None:
                return ["保养计划不存在"]
            if state.get("status") == "retired":
                return ["已停用的保养计划不能登记完成"]
            last_raw, next_raw = payload.get("last_done_date"), payload.get("next_due_date")
            if last_raw and next_raw:
                try:
                    if date.fromisoformat(str(next_raw)) <= date.fromisoformat(str(last_raw)):
                        return ["下次保养到期日必须晚于本次完成日期"]
                except ValueError:
                    return ["保养日期格式非法"]
            return []

        if self.event_type == WO_START_EVENT:
            if state is None:
                return ["维修工单不存在"]
            if state.get("status") in ("completed", "cancelled"):
                return ["已完工或已取消的工单不能开工"]
            return []

        if self.event_type == WO_DONE_EVENT:
            if state is None:
                return ["维修工单不存在"]
            if state.get("status") in ("completed", "cancelled"):
                return ["工单已完工或已取消，不能重复完工"]
            problems: list[str] = []
            if not str(payload.get("action_taken") or "").strip():
                problems.append("工单完工必须填写处理措施（action_taken）")
            dm = payload.get("downtime_minutes")
            if dm is not None:
                try:
                    f = float(dm)
                except (TypeError, ValueError):
                    problems.append("停机时长不是有效数字")
                else:
                    if not math.isfinite(f) or f < 0:
                        problems.append("停机时长必须为非负有限数字")
            return problems

        if self.event_type == EQUIP_RETIRE_EVENT:
            if state is None:
                return ["设备不存在"]
            if state.get("status") == "retired":
                return ["设备已处于报废停用状态"]
            if payload.get("status") != "retired":
                return ["设备报废事件状态必须为 retired"]
            return []

        return []
