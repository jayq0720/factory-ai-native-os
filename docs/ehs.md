# EHS 安全环保与特种作业管理（W25）

EHS 模块覆盖耐火材料工厂的**安全隐患排查闭环、特种作业票管控、事故/险情调查与环保排放监测**，全部业务事实写入不可篡改的事件账本，不新增额外数据库表。

## 1. 本体与配置

- 本体扩展：[packs/refractory/ontology/ehs.yaml](../packs/refractory/ontology/ehs.yaml)
  - 对象：`safety_hazard`（安全隐患）、`work_permit`（特种作业票）、`safety_incident`（安全事故与险情）、`env_reading`（环保监测记录）
  - 事件：`ehs.hazard_reported` / `ehs.hazard_rectified` / `ehs.hazard_verified`、`ehs.permit_issued` / `ehs.permit_closed`、`ehs.incident_reported` / `ehs.incident_closed`、`ehs.env_recorded`
- 行业配置：[packs/refractory/ehs.yaml](../packs/refractory/ehs.yaml)（租户可在 `tenants/<id>/ehs.yaml` 按环评批复覆盖限值）
  - 默认限值：粉尘 `20 mg/m³`、NOx `200 mg/m³`、SO2 `100 mg/m³`、厂界噪声 `85 dB(A)`、VOC `60 mg/m³`
  - `auto_hazard_on_env_exceed: true`：环保监测超标自动开启重大安全隐患并派发待办
  - `max_permit_hours: 72`：特种作业票最长有效期上限
- 审批红线：[tenants/luneng/policy.yaml](../tenants/luneng/policy.yaml)
  - `ehs_high_risk_permit`：动火作业（`hot_work`）与受限空间（`confined_space`）签发自动进入总经理审批（12 小时超时）

## 2. 确定性判定与账本守卫

- 确定性合规计算（`fos/ehs/compliance.py`）：达标判定与隐患闭环率、环保达标率等 KPI 全部由 Python 代码计算，拒绝 `NaN`/`Inf`/负数。
- 账本守卫（`fos/ehs/guards.py`，注册在 `Ledger.add_guard()`）：
  - 未填写整改措施的隐患不能直接复查销项，已销项不能重复操作；
  - 特种作业票安全防护措施必填、有效止时必须晚于起时、时长不得超过 `max_permit_hours`；
  - 事故闭环必须填写根本原因与纠正预防措施；
  - 环保实测值非负、限值必须为正有限数字。

## 3. API、IM 与控制台

- REST API（`/v1/ehs/*`，见 `fos/api/ehs_routes.py`）：
  - 隐患：`GET/POST /v1/ehs/hazards`、`POST /v1/ehs/hazards/{id}/rectify`、`POST /v1/ehs/hazards/{id}/verify`、`POST /v1/ehs/hazards/remind`
  - 作业票：`GET/POST /v1/ehs/permits`（动火/受限空间返回 `202 pending_approval`）、`POST /v1/ehs/permits/{id}/close`
  - 事故：`GET/POST /v1/ehs/incidents`、`POST /v1/ehs/incidents/{id}/close`
  - 环保：`GET/POST /v1/ehs/env`（超标自动建单重大隐患与待办）
  - 汇总与导出：`GET /v1/ehs/summary`、`GET /v1/ehs/report.xlsx`（四表合规报告，仅真实数据）
- IM Fast Path（零模型成本）：发送 `隐患` / `作业票` / `环保` 直接返回对应看板卡片。
- Agent Skill：`ehs_officer`（`packs/refractory/skills/ehs_officer/`），挂接 `ehs.hazards`、`ehs.permits`、`ehs.env_summary`、`ehs.summary` 只读工具。
- Web 控制台：`/app/ehs.html`。
