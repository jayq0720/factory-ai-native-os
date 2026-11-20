"""质量体系账本守卫：注册在 Ledger.add_guard()，任何入口都绕不过去。

- NCR 守卫：已关闭的不合格品不能再修改/处置；未处置不能关闭；重大/严重不合格品关闭前须完成 8D；
- 校准守卫：下次校准到期日必须晚于本次校准日期。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from fos.governance.principal import Principal
from fos.ontology import Ontology
from fos.quality.config import QualityConfig

NCR_OPEN_EVENT = "quality.ncr_opened"
NCR_DISPOSE_EVENT = "quality.ncr_dispositioned"
NCR_8D_EVENT = "quality.ncr_8d_updated"
NCR_CLOSE_EVENT = "quality.ncr_closed"
INST_REG_EVENT = "quality.instrument_registered"
INST_CAL_EVENT = "quality.instrument_calibrated"
INST_STATUS_EVENT = "quality.instrument_status_changed"


class NcrGuard:
    """防止绕过接口关闭未处置或未完成 8D 的不合格品报告。"""

    def __init__(self, event_type: str, configs: dict[str, QualityConfig]):
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
        if state is None:
            return ["不合格品报告不存在"]
        if state.get("status") == "closed":
            return ["不合格品报告已关闭，不能再修改"]
        cfg = self.configs.get(principal.tenant_id) or QualityConfig()
        if self.event_type == NCR_DISPOSE_EVENT:
            disp = payload.get("disposition")
            if disp == "pending":
                return ["处置方式不能选择待定（pending）"]
            return []
        if self.event_type == NCR_CLOSE_EVENT:
            problems: list[str] = []
            if payload.get("status") != "closed":
                problems.append("关闭事件状态必须为 closed")
            disp = state.get("disposition")
            if not disp or disp == "pending":
                problems.append("不合格品尚未完成处置判定，不能关闭")
            sev = state.get("severity")
            if sev in cfg.ncr.require_8d_severities:
                if state.get("d8_status") != "completed":
                    problems.append(f"严重程度为 {sev} 的不合格品必须完成 8D 整改后才能关闭")
                if not (state.get("root_cause") or "").strip():
                    problems.append("关闭前必须填写根本原因（D4）")
                if not (state.get("corrective_action") or "").strip():
                    problems.append("关闭前必须填写纠正措施（D5/D6）")
            return problems
        return []


class CalibrationGuard:
    """计量器具校准日期合法性校验：下次到期日必须晚于本次校准日期。"""

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
        last_raw = payload.get("last_calibrated_on")
        next_raw = payload.get("next_due_date")
        if last_raw and next_raw:
            try:
                last_d = date.fromisoformat(str(last_raw))
                next_d = date.fromisoformat(str(next_raw))
            except ValueError:
                return ["校准日期格式非法"]
            if next_d <= last_d:
                return ["下次校准到期日必须晚于本次校准日期"]
        return []
