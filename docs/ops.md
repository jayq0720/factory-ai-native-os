# 全厂统一 Watchdog 巡检、SaaS 运营后台与新工厂复制验证（W28）

W28 面向 M5「全职能上线、可复制」目标，提供跨模块统一巡检、多租户 SaaS 运营总览、安全基线自检与第二家工厂零代码复制校验。

## 1. 全厂统一 Watchdog 巡检（`fos/ops/watchdog.py`）

- 单次幂等触发当前租户所有已启用模块的到期提醒（计量校准、财务逾期应收/应付、设备保养、EHS 隐患超期、HR 特种作业证照临期），并自动将超过 `expires_at` 的待审批单标记为 `expired`。
- 同一天多次触发严格去重（第二次新建待办数为 0），可直接接入定时任务或通过 `POST /v1/ops/watchdog/run` 手动触发。

## 2. SaaS 运营后台与安全基线自检（`fos/ops/service.py`）

- **租户健康快照（`GET /v1/ops/health`）**：统计活跃用户、事件总量与 7 日增量、投影对象数、未闭环待办、待审批数、7 日 Trigger 与 LLM Token 用量、已启用模块列表。
- **安全基线自检（`GET /v1/ops/security`）**：校验本体加载、策略规则、IM 信道配置、生产环境 `FOS_EDGE_COMMAND_KEY` 签名私钥与禁止模拟 LLM。
- **多租户 SaaS 总览（`GET /v1/ops/tenants`）**：仅 `*.*:*` 平台超级管理员可查看所有接入工厂的运营汇总与安全状态。
- **Web 控制台**：`/app/ops.html`。

## 3. 第二家工厂零代码复制验证（`GET /v1/ops/replication/{tenant_id}`）

- 新建工厂只需编写一份 `tenants/<new_id>/tenant.yaml`（指定 `industry_pack: refractory` 与岗位角色），即可零代码继承全部 12 层耐材行业本体、策略规则、全部 Skill 与业务模块；
- 通过 `verify_tenant_replication()` 或 `/v1/ops/replication/{tenant_id}` 一键校验配置完整性，确保单厂部署 ≤ 1 天。
