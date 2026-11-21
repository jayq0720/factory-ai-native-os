"""研发配方与试验账本守卫：注册在 Ledger.add_guard()，任何入口都绕不过去。

- 配方版本创建：配比总和必须在 100% ± pct_tolerance，烧成温度在合理范围，原料 JSON 合法非空；
- 配方定型放行量产：不能重复放行或放行已作废配方，且必须至少存在配置要求数量的合格试验记录；
- 配方作废：不能作废已定型或已作废配方。
"""

from __future__ import annotations

import json
import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import ObjectState
from fos.governance.principal import Principal
from fos.ontology import Ontology
from fos.rnd.config import RndConfig

PROJ_CREATE_EVENT = "rnd.project_created"
PROJ_UPDATE_EVENT = "rnd.project_updated"
FORMULA_CREATE_EVENT = "rnd.formula_created"
FORMULA_RELEASE_EVENT = "rnd.formula_released"
FORMULA_REJECT_EVENT = "rnd.formula_rejected"
TRIAL_LOG_EVENT = "rnd.trial_logged"


class RndGuard:
    """研发配方与试验账本不变量守卫。"""

    def __init__(self, event_type: str, configs: dict[str, RndConfig]):
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
        if self.event_type == FORMULA_CREATE_EVENT:
            problems: list[str] = []
            tol = cfg.pct_tolerance if cfg is not None else 0.5
            min_t = cfg.min_firing_temp_c if cfg is not None else 100.0
            max_t = cfg.max_firing_temp_c if cfg is not None else 1900.0
            try:
                tot = float(payload.get("total_pct"))  # type: ignore[arg-type]
                if not math.isfinite(tot) or abs(tot - 100.0) > tol:
                    problems.append(f"配方组分总和 {tot:g}% 超出 100% ± {tol:g}% 允许范围")
            except (TypeError, ValueError):
                problems.append("配方组分总和（total_pct）不是有效数字")
            try:
                temp = float(payload.get("firing_temp_c"))  # type: ignore[arg-type]
                if not math.isfinite(temp) or not (min_t <= temp <= max_t):
                    problems.append(f"烧成温度 {temp:g}℃ 超出 [{min_t:g}, {max_t:g}]℃ 范围")
            except (TypeError, ValueError):
                problems.append("烧成温度（firing_temp_c）不是有效数字")
            raw_ing = payload.get("ingredients_json")
            try:
                parsed = json.loads(str(raw_ing or ""))
                if not isinstance(parsed, list) or not parsed:
                    problems.append("配方原料明细（ingredients_json）必须为非空 JSON 数组")
            except ValueError:
                problems.append("配方原料明细（ingredients_json）JSON 格式非法")
            return problems

        if self.event_type == FORMULA_RELEASE_EVENT:
            if state is None:
                return ["配方版本不存在"]
            if state.get("status") in ("approved", "rejected"):
                return ["配方已定型或已作废，不能重复放行"]
            if payload.get("status") != "approved":
                return ["配方定型放行状态必须为 approved"]
            min_ok = cfg.min_passed_trials_for_release if cfg is not None else 1
            rows = (
                await session.scalars(
                    select(ObjectState).where(
                        ObjectState.tenant_id == principal.tenant_id,
                        ObjectState.object_type == "lab_trial",
                        ObjectState.data_mode == data_mode,
                    )
                )
            ).all()
            passed_cnt = sum(
                1 for o in rows if o.state.get("formula") == object_id and o.state.get("passed") is True
            )
            if passed_cnt < min_ok:
                return [f"配方尚未通过至少 {min_ok} 次合格试验（当前合格 {passed_cnt} 次），禁止放行量产"]
            return []

        if self.event_type == FORMULA_REJECT_EVENT:
            if state is None:
                return ["配方版本不存在"]
            if state.get("status") in ("approved", "rejected"):
                return ["配方已定型或已作废，不能重复作废"]
            if payload.get("status") != "rejected":
                return ["配方作废状态必须为 rejected"]
            return []

        return []
