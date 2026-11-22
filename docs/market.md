# 经营决策驾驶舱与市场商机线索（W27c）

经营与市场模块补齐 Phase 3 最后两块业务拼图：**市场商机漏斗/竞品情报**与**全厂跨部门经营决策驾驶舱**，全部基于事件账本，不新增数据库表。

## 1. 本体与配置

- 本体扩展：[packs/refractory/ontology/market.yaml](../packs/refractory/ontology/market.yaml)
  - 对象：`market_lead`（市场商机线索）、`competitor_intel`（竞品行情情报）
  - 事件：`market.lead_created` / `market.lead_updated` / `market.lead_closed`、`market.intel_logged`
- 行业配置：[packs/refractory/market.yaml](../packs/refractory/market.yaml)
  - 各阶段默认赢单概率（`new: 10%`、`qualified: 30%`、`quoted: 50%`、`negotiation: 75%`、`won: 100%`、`lost: 0%`）与大额商机阈值 `500000` 元。
- 审批红线：[tenants/luneng/policy.yaml](../tenants/luneng/policy.yaml)
  - `market_large_lead_lost`：50 万元及以上大额商机丢单关闭自动进入销售负责人（`sales_lead`）复盘确认（48 小时超时）。

## 2. 确定性分析、跨部门驾驶舱与守卫

- 确定性商机漏斗与经营驾驶舱（`fos/market/analytics.py`）：
  - 确定性计算商机原始金额、加权预测额、赢单额与赢单率；
  - 经营驾驶舱跨模块聚合实时账本投影：财务应收/应付余额、质量未闭环 NCR、设备未完修工单、EHS 未销项/重大隐患、人力在职人数、研发在研项目/已定型配方、筑炉在建项目与市场商机漏斗。
- 账本守卫（`fos/market/guards.py`）：预计金额非负有限、赢单概率在 `[0, 100]`，已赢单/已丢单商机禁止再次修改或重复结案。

## 3. API、IM 与控制台

- REST API（`/v1/market/*`，见 `fos/api/market_routes.py`）：
  - 商机：`GET/POST /v1/market/leads`、`PATCH /v1/market/leads/{id}`、`POST /v1/market/leads/{id}/close`
  - 竞品：`GET/POST /v1/market/intel`
  - 驾驶舱与导出：`GET /v1/market/cockpit`、`GET /v1/market/report.xlsx`（三表 Excel，仅真实数据）
- IM Fast Path（零模型成本）：发送 `经营` / `驾驶舱`、`商机`、`竞品` 直接返回对应看板卡片。
- Agent Skill：`executive_advisor`（`packs/refractory/skills/executive_advisor/`），挂接 `market.cockpit`、`market.funnel`、`market.intel` 只读工具。
- Web 控制台：`/app/market.html`。
