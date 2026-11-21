"""人力、排班与特种资质账本守卫：注册在 Ledger.add_guard()，任何入口都绕不过去。

- 特种资质：到期日必须晚于发证日；已吊销证照不能续期，续期到期日须晚于原到期日；
- 排班考勤：单班工时不得超过 max_shift_hours，加班/计件/单价必须为非负有限数字；
- 请假单：结束日期不得早于开始日期，请假天数必须大于 0；已撤销请假单不能重复撤销。
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from fos.governance.principal import Principal
from fos.hr.config import HrConfig
from fos.ontology import Ontology

EMP_REG_EVENT = "hr.employee_registered"
EMP_UPD_EVENT = "hr.employee_updated"
CERT_REC_EVENT = "hr.cert_recorded"
CERT_RENEW_EVENT = "hr.cert_renewed"
ROSTER_LOG_EVENT = "hr.roster_logged"
LEAVE_SUB_EVENT = "hr.leave_submitted"
LEAVE_CANCEL_EVENT = "hr.leave_cancelled"


def _parse_date(raw: Any) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


class HrGuard:
    """人力与特种资质账本不变量守卫。"""

    def __init__(self, event_type: str, configs: dict[str, HrConfig]):
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
        if self.event_type == CERT_REC_EVENT:
            d_iss = _parse_date(payload.get("issued_date"))
            d_exp = _parse_date(payload.get("expires_date"))
            if d_iss is None or d_exp is None:
                return ["证照发证/到期日期格式非法"]
            if d_exp <= d_iss:
                return ["证照到期日必须晚于发证日期"]
            return []

        if self.event_type == CERT_RENEW_EVENT:
            if state is None:
                return ["特种作业资质不存在"]
            if state.get("status") == "revoked":
                return ["已吊销的证照不能复审换证"]
            d_old = _parse_date(state.get("expires_date"))
            d_new = _parse_date(payload.get("expires_date"))
            if d_new is None:
                return ["新的到期日期格式非法"]
            if d_old is not None and d_new <= d_old:
                return ["复审换证的新到期日必须晚于原到期日"]
            return []

        if self.event_type == ROSTER_LOG_EVENT:
            cfg = self.configs.get(principal.tenant_id)
            max_h = cfg.max_shift_hours if cfg is not None else 16.0
            problems: list[str] = []
            for key, label, cap in (
                ("hours", "出勤工时", max_h),
                ("overtime_hours", "加班工时", max_h),
                ("piece_qty", "计件产量", None),
                ("piece_rate", "计件单价", None),
            ):
                raw = payload.get(key)
                if raw is None:
                    continue
                try:
                    f = float(raw)
                except (TypeError, ValueError):
                    problems.append(f"{label}不是有效数字")
                    continue
                if not math.isfinite(f) or f < 0:
                    problems.append(f"{label}必须为非负有限数字")
                elif cap is not None and f > cap:
                    problems.append(f"{label} {f:g} 超过单班上限 {cap:g} 小时")
            return problems

        if self.event_type == LEAVE_SUB_EVENT:
            problems = []
            d_start = _parse_date(payload.get("start_date"))
            d_end = _parse_date(payload.get("end_date"))
            if d_start is None or d_end is None:
                problems.append("请假起止日期格式非法")
            elif d_end < d_start:
                problems.append("请假结束日期不能早于开始日期")
            raw_days = payload.get("days")
            try:
                days = float(raw_days)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                problems.append("请假天数不是有效数字")
            else:
                if not math.isfinite(days) or days <= 0:
                    problems.append("请假天数必须大于 0")
            return problems

        if self.event_type == LEAVE_CANCEL_EVENT:
            if state is None:
                return ["请假单不存在"]
            if state.get("status") == "cancelled":
                return ["请假单已撤销，不能重复撤销"]
            if payload.get("status") != "cancelled":
                return ["撤销请假单状态必须为 cancelled"]
            return []

        return []
