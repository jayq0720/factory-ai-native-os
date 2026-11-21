# 人力、排班、计件工资与特种资质管理（W26）

人力模块覆盖耐火材料工厂的**员工花名册、特种作业证照复审预警、班次考勤与计件产量、请假审批与月度确定性薪酬核算**，全部基于事件账本，不新增数据库表。

## 1. 本体与配置

- 本体扩展：[packs/refractory/ontology/hr.yaml](../packs/refractory/ontology/hr.yaml)
  - 对象：`employee`（员工档案）、`work_cert`（特种作业资质）、`shift_roster`（排班与计件考勤）、`leave_request`（请假单）
  - 事件：`hr.employee_registered` / `hr.employee_updated`、`hr.cert_recorded` / `hr.cert_renewed`、`hr.roster_logged`、`hr.leave_submitted` / `hr.leave_cancelled`
- 行业配置：[packs/refractory/hr.yaml](../packs/refractory/hr.yaml)
  - `cert_remind_days: 30`、`standard_work_days: 22`、`overtime_multiplier: 1.5`、`default_piece_rate: 12.0`、`max_shift_hours: 16.0`
- 审批红线：[tenants/luneng/policy.yaml](../tenants/luneng/policy.yaml)
  - `hr_long_leave`：3 天及以上请假自动进入总经理审批（48 小时超时）

## 2. 确定性薪酬核算与账本守卫

- 确定性薪酬核算（`fos/hr/payroll.py`）：按月汇总底薪折算、加班费与计件工资，拒绝 `NaN`/`Inf`/负数，绝不让模型计算薪资。
- 账本守卫（`fos/hr/guards.py`，注册在 `Ledger.add_guard()`）：
  - 证照到期日须晚于发证日；已吊销证照禁止续期，复审换证的新到期日须晚于原到期日；
  - 单班工时/加班工时不得超过 `max_shift_hours`，计件产量与单价非负有限；
  - 请假结束日期不得早于开始日期且天数大于 0，已撤销请假单不能重复撤销。

## 3. API、IM 与控制台

- REST API（`/v1/hr/*`，见 `fos/api/hr_routes.py`）：
  - 员工：`GET/POST /v1/hr/employees`、`PATCH /v1/hr/employees/{id}`
  - 证照：`GET/POST /v1/hr/certs`、`POST /v1/hr/certs/{id}/renew`、`POST /v1/hr/certs/remind`
  - 排班计件：`GET/POST /v1/hr/rosters`
  - 请假：`GET/POST /v1/hr/leaves`（3 天及以上返回 `202 pending_approval`）、`POST /v1/hr/leaves/{id}/cancel`
  - 薪酬与导出：`GET /v1/hr/payroll?month=YYYY-MM`、`GET /v1/hr/report.xlsx?month=YYYY-MM`（四表 Excel，仅真实数据）
- IM Fast Path（零模型成本）：发送 `排班` / `资质` / `工资` 直接返回对应看板卡片。
- Agent Skill：`hr_assistant`（`packs/refractory/skills/hr_assistant/`），挂接 `hr.employees`、`hr.certs`、`hr.roster`、`hr.payroll` 只读工具。
- Web 控制台：`/app/hr.html`。
