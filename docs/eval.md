# 黄金数据集评测与 Badcase 回归引擎（W29）

W29 对应架构第 5 层（调优层：黄金数据集与 Badcase 回归），提供零模型成本、可进 CI 门禁的全厂确定性回归评测套件。

## 1. 黄金评测集（[golden_suite.yaml](../packs/refractory/eval/golden_suite.yaml)）

包含四大核心评测维度的 49 条标准用例：

1. **Fast Path 分流回归（`fast_path_routing`）**：覆盖设备、保养、工单、备件、隐患、作业票、环保、排班、资质、工资、研发、配方、试验、施工、施工日报、烘炉、驾驶舱、商机、竞品、应收、应付、不合格及自然语言兜底转 Router。
2. **Skill 路由回归（`skill_routing`）**：覆盖安全环保、人力薪酬、研发配方、筑炉施工、经营决策、设备管理、财务、质检等 8 大岗位 Skill 及寒暄不误触发校验。
3. **核心确定性算子回归（`deterministic_calculators`）**：验证环保达标、计件薪酬、耐材配方 Al₂O₃ 与吨成本、烘炉曲线告警、商机漏斗加权预测的确定性计算精度。
4. **审批红线回归（`policy_redlines`）**：验证动火作业票、3 天及以上请假、研发配方定型、带尾项施工验收、50 万以上商机丢单、A 类设备报废、5 万以上大额付款全部稳定命中审批，低风险单据直接放行。

## 2. 运行方式

- HTTP 接口：`GET /v1/ops/eval`（需管理员权限 `platform.ops:admin`），返回分类通过率与 `badcases` 列表。
- 自动化门禁：`tests/test_eval.py` 在每次 PR CI 中自动校验 `all_passed == True` 且 `pass_rate_pct == 100.0`。
