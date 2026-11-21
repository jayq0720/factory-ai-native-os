# 研发配方试验与新产品试产管理（W27）

研发模块覆盖耐火材料工厂的**新产品立项、配方版本确定性核算、小试/中试/窑炉试烧理化判定与配方定型审批门禁**，全部基于事件账本，不新增数据库表。

## 1. 本体与配置

- 本体扩展：[packs/refractory/ontology/rnd.yaml](../packs/refractory/ontology/rnd.yaml)
  - 对象：`rnd_project`（研发项目）、`formula_version`（配方版本）、`lab_trial`（试验检测记录）
  - 事件：`rnd.project_created` / `rnd.project_updated`、`rnd.formula_created` / `rnd.formula_released` / `rnd.formula_rejected`、`rnd.trial_logged`
- 行业配置：[packs/refractory/rnd.yaml](../packs/refractory/rnd.yaml)
  - `pct_tolerance: 0.5`（组分总和须满足 `100% ± 0.5%`）、`min_firing_temp_c: 100`、`max_firing_temp_c: 1900`、`min_passed_trials_for_release: 1`
- 审批红线：[tenants/luneng/policy.yaml](../tenants/luneng/policy.yaml)
  - `rnd_formula_release`：新配方定型放行量产自动进入总经理审批（48 小时超时）

## 2. 确定性核算与账本守卫

- 确定性配方与试验判定（`fos/rnd/formulation.py`）：确定性计算配方总配比、加权理论 `Al2O3(%)`、估算吨原料成本，并对照项目理化指标下限判定试验是否达标。
- 账本守卫（`fos/rnd/guards.py`，注册在 `Ledger.add_guard()`）：
  - 配方总配比超出容差、烧成温度越界或原料 JSON 非法直接拒绝入账；
  - 配方定型放行量产前必须至少通过配置要求次数的合格试验（`lab_trial.passed == true`），否则拒绝进入审批/放行；
  - 已定型或已作废配方禁止重复放行或作废。

## 3. API、IM 与控制台

- REST API（`/v1/rnd/*`，见 `fos/api/rnd_routes.py`）：
  - 项目：`GET/POST /v1/rnd/projects`、`PATCH /v1/rnd/projects/{id}`
  - 配方：`GET/POST /v1/rnd/formulas`、`POST /v1/rnd/formulas/{id}/release`（返回 `202 pending_approval`）、`POST /v1/rnd/formulas/{id}/reject`
  - 试验：`GET/POST /v1/rnd/trials`
  - 导出：`GET /v1/rnd/report.xlsx`（三表 Excel，仅真实数据）
- IM Fast Path（零模型成本）：发送 `研发` / `配方` / `试验` 直接返回对应看板卡片。
- Agent Skill：`rnd_engineer`（`packs/refractory/skills/rnd_engineer/`），挂接 `rnd.projects`、`rnd.formulas`、`rnd.trials` 只读工具。
- Web 控制台：`/app/rnd.html`。
