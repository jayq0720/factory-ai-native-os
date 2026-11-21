"""EHS 账本守卫：注册在 Ledger.add_guard()，任何入口都绕不过去。

- 隐患整改与销项：已销项不能重复操作；未填写整改措施不能直接复查销项；
- 特种作业票：安全措施必填、有效止时必须晚于起时、作业时长不超过配置上限；已关闭不能重复关闭；
- 事故闭环：未填根因与纠正预防措施不能闭环，已闭环不能重复关闭；
- 环保监测：实测值与限值必须为合法非负/正有限数字。
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from fos.ehs.config import EhsConfig
from fos.governance.principal import Principal
from fos.ontology import Ontology

HAZARD_REPORT_EVENT = "ehs.hazard_reported"
HAZARD_RECTIFY_EVENT = "ehs.hazard_rectified"
HAZARD_VERIFY_EVENT = "ehs.hazard_verified"
PERMIT_ISSUE_EVENT = "ehs.permit_issued"
PERMIT_CLOSE_EVENT = "ehs.permit_closed"
INCIDENT_REPORT_EVENT = "ehs.incident_reported"
INCIDENT_CLOSE_EVENT = "ehs.incident_closed"
ENV_RECORD_EVENT = "ehs.env_recorded"


def _parse_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


class EhsGuard:
    """EHS 账本不变量守卫。"""

    def __init__(self, event_type: str, configs: dict[str, EhsConfig]):
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
        if self.event_type == HAZARD_RECTIFY_EVENT:
            if state is None:
                return ["安全隐患不存在"]
            if state.get("status") == "verified":
                return ["已复查销项的隐患不能再登记整改"]
            if not str(payload.get("rectification_plan") or "").strip():
                return ["登记隐患整改必须填写整改措施（rectification_plan）"]
            return []

        if self.event_type == HAZARD_VERIFY_EVENT:
            if state is None:
                return ["安全隐患不存在"]
            if state.get("status") == "verified":
                return ["隐患已复查销项，不能重复销项"]
            problems: list[str] = []
            if not str(state.get("rectification_plan") or "").strip():
                problems.append("隐患尚未登记整改措施，不能直接复查销项")
            if payload.get("status") != "verified":
                problems.append("隐患复查销项状态必须为 verified")
            return problems

        if self.event_type == PERMIT_ISSUE_EVENT:
            problems = []
            if not str(payload.get("safety_measures") or "").strip():
                problems.append("特种作业票必须填写安全防护措施（safety_measures）")
            dt_from = _parse_dt(payload.get("valid_from"))
            dt_until = _parse_dt(payload.get("valid_until"))
            if dt_from is None or dt_until is None:
                problems.append("作业票有效起止时间格式非法")
            elif dt_until <= dt_from:
                problems.append("作业票有效止时必须晚于有效起时")
            else:
                cfg = self.configs.get(principal.tenant_id)
                max_h = cfg.max_permit_hours if cfg is not None else 72
                hours = (dt_until - dt_from).total_seconds() / 3600.0
                if hours > max_h:
                    problems.append(f"特种作业票有效期 {hours:.1f} 小时，超过允许上限 {max_h} 小时")
            return problems

        if self.event_type == PERMIT_CLOSE_EVENT:
            if state is None:
                return ["特种作业票不存在"]
            if state.get("status") in ("closed", "revoked"):
                return ["作业票已关闭或已撤销，不能重复操作"]
            if payload.get("status") not in ("closed", "revoked"):
                return ["作业票关闭状态必须为 closed 或 revoked"]
            return []

        if self.event_type == INCIDENT_CLOSE_EVENT:
            if state is None:
                return ["安全事故记录不存在"]
            if state.get("status") == "closed":
                return ["安全事故已闭环，不能重复关闭"]
            problems = []
            if not str(payload.get("root_cause") or "").strip():
                problems.append("事故闭环必须填写根本原因（root_cause）")
            if not str(payload.get("corrective_action") or "").strip():
                problems.append("事故闭环必须填写纠正预防措施（corrective_action）")
            if payload.get("status") != "closed":
                problems.append("事故闭环状态必须为 closed")
            return problems

        if self.event_type == ENV_RECORD_EVENT:
            problems = []
            for key, label, allow_zero in (("value", "实测值", True), ("limit_value", "排放限值", False)):
                raw = payload.get(key)
                try:
                    f = float(raw)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    problems.append(f"{label}不是有效数字")
                else:
                    if not math.isfinite(f) or (f < 0 if allow_zero else f <= 0):
                        problems.append(f"{label}超出合法数值范围")
            return problems

        return []
