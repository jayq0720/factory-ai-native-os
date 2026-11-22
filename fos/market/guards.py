"""市场商机账本守卫：注册在 Ledger.add_guard()，任何入口都绕不过去。

- 商机创建/更新：预计金额非负有限、赢单概率在 [0, 100]；已结案（won/lost）商机禁止再修改；
- 商机结案：只能流转至 won 或 lost，已结案商机禁止重复结案。
"""

from __future__ import annotations

import math
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from fos.governance.principal import Principal
from fos.ontology import Ontology

LEAD_CREATE_EVENT = "market.lead_created"
LEAD_UPDATE_EVENT = "market.lead_updated"
LEAD_CLOSE_EVENT = "market.lead_closed"
INTEL_LOG_EVENT = "market.intel_logged"


class MarketGuard:
    """市场商机账本不变量守卫。"""

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
        if self.event_type == LEAD_CREATE_EVENT:
            return _check_lead_nums(payload)

        if self.event_type == LEAD_UPDATE_EVENT:
            if state is None:
                return ["市场商机不存在"]
            if state.get("stage") in ("won", "lost"):
                return ["已赢单或已丢单的商机不能继续修改"]
            return _check_lead_nums(payload)

        if self.event_type == LEAD_CLOSE_EVENT:
            if state is None:
                return ["市场商机不存在"]
            if state.get("stage") in ("won", "lost"):
                return ["商机已结案，不能重复关闭"]
            if payload.get("stage") not in ("won", "lost"):
                return ["商机结案阶段必须为 won 或 lost"]
            return []

        return []


def _check_lead_nums(payload: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if "est_amount" in payload and payload["est_amount"] is not None:
        try:
            f = float(payload["est_amount"])
            if not math.isfinite(f) or f < 0:
                problems.append("商机预计金额必须为非负有限数字")
        except (TypeError, ValueError):
            problems.append("商机预计金额不是有效数字")
    if "probability" in payload and payload["probability"] is not None:
        try:
            p = int(payload["probability"])
            if not (0 <= p <= 100):
                problems.append("赢单概率必须在 0~100 之间")
        except (TypeError, ValueError):
            problems.append("赢单概率不是有效整数")
    return problems
