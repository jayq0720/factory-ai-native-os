# 设备管理 EAM（W24）

对应路线图 W24（设备台账、点检/保养计划、故障报修与备件联动、可用率与 MTBF/MTTR 统计）。

## 设计要点

1. **账本唯一真相**：在核心本体 `equipment` 上扩展设备编码、类别、关键等级（A/B/C）与保养周期，并新增 `maintenance_plan`、`work_order`、`inspection_log` 三个对象及 8 个事件，零新增数据库表。
2. **账本守卫（`fos/eam/guards.py`）**：
   - 保养完成时下次到期日必须晚于本次完成日期；
   - 维修工单严禁重复开工/完工，完工必须填写处理措施且停机时长非负有限；
   - 已报废设备不能重复报废或开立维修工单。
3. **关键设备报废审批（`tenants/luneng/policy.yaml`）**：A 类关键设备报废停用自动触发总经理（`gm`）审批，返回 `202`，审批通过后入账。
4. **点检与工单闭环**：日常点检登记 `abnormal` 时自动开立高优先级维修工单并生成待办；高/紧急故障自动将设备置为 `maintenance`，完工后自动恢复 `running`。
5. **备件联动与可靠性指标**：备件看板直接投影仓储账本中 `category == spare_part` 的现存与安全库存预警；`fos/eam/metrics.py` 确定性计算可用率、MTTR 与 MTBF。

## 接口与入口

- REST API：`/v1/eam/{equipment,pm,pm/remind,work-orders,inspections,spare-parts,reliability,report/export}`
- Web 控制台：`/app/eam.html`
- IM Fast Path：`设备`、`保养`、`工单` / `报修`、`备件`
- 只读 Agent 工具与 Skill：`equipment_manager`（`eam.equipment`、`eam.pm_due`、`eam.work_orders`、`eam.reliability`）
