# 筑炉施工与烘炉验收管理（W27b）

筑炉施工模块覆盖耐火材料工厂客户现场的**筑炉施工立项、每日施工日报与砌筑进度自动重算、烘炉升温曲线防爆裂监测与竣工验收管控**，全部基于事件账本，不新增数据库表。

## 1. 本体与配置

- 本体扩展：[packs/refractory/ontology/construction.yaml](../packs/refractory/ontology/construction.yaml)
  - 对象：`site_project`（筑炉施工项目）、`site_log`（施工日报）、`dryout_record`（烘炉升温记录）
  - 事件：`construction.site_created` / `construction.site_updated` / `construction.site_accepted`、`construction.log_recorded`、`construction.dryout_logged`
- 行业配置：[packs/refractory/construction.yaml](../packs/refractory/construction.yaml)
  - `max_temp_deviation_c: 25.0`、`max_ramp_rate_c_per_h: 35.0`、`min_progress_for_accept_pct: 95.0`
- 审批红线：[tenants/luneng/policy.yaml](../tenants/luneng/policy.yaml)
  - `construction_conditional_accept`：带尾项/让步条件竣工验收（`acceptance_type == conditional`）自动进入总经理审批（48 小时超时）

## 2. 确定性计算与账本守卫

- 确定性进度与烘炉校验（`fos/construction/metrics.py`）：根据施工日报累计砌筑吨位自动重算进度百分比，确定性判定烘炉实测温差与升温速率是否超限（超限自动派发防爆裂预警待办）。
- 账本守卫（`fos/construction/guards.py`，注册在 `Ledger.add_guard()`）：
  - 计划砌筑量须大于 0，计划竣工日不得早于开工日；
  - 施工日报作业人数须 ≥ 1，完成吨位非负有限；
  - 正常竣工验收前进度必须 ≥ `min_progress_for_accept_pct`（95%），未达标须补齐进度或改走带尾项条件验收；已验收项目禁止重复验收。

## 3. API、IM 与控制台

- REST API（`/v1/construction/*`，见 `fos/api/construction_routes.py`）：
  - 项目：`GET/POST /v1/construction/sites`、`PATCH /v1/construction/sites/{id}`、`POST /v1/construction/sites/{id}/accept`
  - 日报：`GET/POST /v1/construction/logs`
  - 烘炉：`GET/POST /v1/construction/dryout`
  - 导出：`GET /v1/construction/report.xlsx`（三表 Excel，仅真实数据）
- IM Fast Path（零模型成本）：发送 `施工` / `施工日报` / `烘炉` 直接返回对应看板卡片。
- Agent Skill：`site_supervisor`（`packs/refractory/skills/site_supervisor/`），挂接 `construction.sites`、`construction.logs`、`construction.dryout` 只读工具。
- Web 控制台：`/app/construction.html`。
