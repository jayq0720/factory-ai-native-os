# 计划与排窑（W16，里程碑 M3 的第二段）

对应开发方案 5.3 “计划（PMC）”和 5.4 主干里的“计划员评估交期、排窑 → 缺料 → 采购”：

- 新订单到来，计划员助理自动把它放进排窑求解器重排一次，告诉计划员**能不能按期、会把哪些订单往后推、缺什么料**；
- 计划员一键生成**排窑建议**（求解器），在网页上核对后**确认排窑计划**，勾选的订单同时**承诺交期**；
- 按确认的计划算**物料需求**，有缺口推给采购，采购在“请购建议”里一键生成请购单（W17，见 `docs/supply.md`）。

求解交给 OR-Tools CP-SAT（Apache-2.0，方案 5.5）；模型只负责解释结果，不改数（CLAUDE.md 规则 3）。

## 一条完整的流程

```
主数据：窑炉 pmc.kiln_capacity_set（每窑装量、单位、烧成周期、能否排产）
        产品 pmc.product_firing_set（烧成温度、产品烧成周期、烧成组）
        配方用量 rnd.recipe_material_set（当前配方版本每单位产品用多少原料、损耗率）
        这三类都可以在主数据导入模板里一起填（窑炉、产品工作表多了几列）
                 │
POST /v1/planning/draft          排窑建议（不入账）：计划窑次、订单预计可发货、窑炉利用率、原料缺口
POST /v1/planning/plans          确认（可以改过再提交）：pmc.kiln_plan_confirmed + 每行 pmc.run_planned，全有或全无
        │                        旧版本记 pmc.kiln_plan_superseded；勾选的订单记 pmc.delivery_promised（承诺交期）
        │                        有原料缺口：事务提交后推给采购（planning.yaml 的 shortage_notify_roles）
GET  /v1/planning/plan           当前确认的计划
GET  /v1/planning/materials      按当前计划算的物料需求和缺口
POST /v1/planning/evaluate       交期评估：{order} 已有订单，或 {candidate: {product, qty, unit, due_date}} 假设的新订单
```

网页：`/app/plan.html`（甘特图 + 订单表 + 原料表 + 交期评估）。

IM（只读 Fast Path，零模型成本，需要 `pmc.kiln_plan:read`）：
- 「排窑」「排窑计划」：当前计划的窑次和有风险的订单；
- 「缺料」：按当前计划的原料缺口；
- 「交期评估 HT2026-018-1」「能不能交 …」：把这个订单放进去重排的结果。

主动推送：`fos channels run` 进程每 5 分钟看一次最近 24 小时新建的订单，每个订单评估一次（Trigger 唯一约束保证只推一次），
卡片推给计划员（`planning.yaml` 的 `watch`）。

自然语言问“这单 3 天交 50 吨能不能接”走计划员 Skill（`packs/refractory/skills/kiln_planner`），工具 `planning.evaluate`、
`planning.current`、`planning.shortages`（只读）。

## 本体（packs/refractory/ontology/planning.yaml）

| 对象 | 说明 |
|---|---|
| `kiln_plan` 排窑计划 | 一版计划：计划编号、计划开始 / 结束、状态（已确认 / 已替代）、求解结果 |
| `planned_run` 计划装窑行 | 一个计划窑次里给一个订单装多少；同窑次多个订单多行（窑次号、时间相同）；带预计可发货日期 |
| `recipe_material` 配方用量 | 配方版本 × 物料：单位产品用量、用量单位、损耗率（研发维护，`rnd.recipe:approve`） |
| 扩展 `kiln` | 每窑装量、装量单位、烧成周期（含装出窑、冷却）、排产状态（可排产 / 维修 / 停止排产） |
| 扩展 `product` | 烧成温度、产品烧成周期（不填按窑炉周期）、烧成组（同组产品可以同窑装烧） |
| 扩展 `sales_order` | 承诺交期 `promised_date`（客户要求的交期仍是 `due_date`） |

隧道窑是连续的，按“烧成周期”切成段（例如 24 小时一段、每段装量 = 一天的推进量），和梭式窑用同一个模型。

## 求解模型（fos/planning/solver.py）

- 时间按小时，从下一个整点开始，排 `horizon_days` 天；数量精度 0.01；
- 每个窑一串有序窑次：前一窑出窑后一窑才能装；正在烧的窑（`kiln_run` 状态 firing）按开始时间 + 周期之后才空；
- 每个窑次只装一个烧成组；时长 = 该组产品在这个窑上的烧成周期（取组内最长）；
- 订单可以拆到多个窑次，一个窑次可以装多个同组订单，合计不超过每窑装量；
- 订单待烧量 = 订单数量 − max(已装窑 / 已完工批次, 已发货)；其中“已开工未装窑”的批次马上能装，
  其余要先配料、成型、干燥（`pre_firing_hours`）；出窑到可发货要 `post_firing_hours`（冷却、检验、包装）；
- 能不能进这个窑：单位一致（`orders.yaml` 的同义词归一，不换算），产品烧成温度 ≤ 窑炉最高温度；
- 目标：排不下的量 ≫ 逾期小时 > 开窑数 > 完成时间、拆单（权重在 `planning.yaml`）；
- 到时限（默认 10 秒）还不是最优时返回目前最好的可行解，并提示。

## 确认时的账本守卫（fos/planning/guard.py）

`pmc.run_planned` 注册为账本守卫：不论从计划接口、`/v1/events` 还是以后的卡片确认写入，每一行都再查一次：
窑炉不在维修 / 停用且设置了装量；单位一致；窑温够；订单未关闭、产品一致；同一窑次时间一致、同一烧成组、合计不超装；
同一窑上不同窑次不重叠。有一行不过，整批回滚。

## 物料需求（fos/planning/materials.py）

需求 = 计划装窑行里还没开工的量 × 当前配方版本（版本号最大）的单位用量 × (1 + 损耗率)，在装窑前 `pre_firing_hours` 要到位；
订单里已开工的批次先抵扣。供给 = 现存（`stock_statuses` 的原料批次的实时库存）+ 已到货待检 + 采购单未到量（按采购单交期到位）。
按时间线累计，第一次不够的日期就是“哪天起要用”，最大缺口就是缺多少；低于安全库存单独标出。

**口径说明**：现存 = 到货 − 领用 + 退料 ± 盘点（W17 仓储，`fos/supply/stock.py`）；采购单已到的部分算在批次里，不重复计入在途。
缺口接到采购的请购建议，见 `docs/supply.md`。

## 配置（packs/refractory/planning.yaml，全部是占位值）

| 项 | 默认 | 说明 |
|---|---|---|
| `horizon_days` | 14 | 排多少天 |
| `pre_firing_hours` | 48 | 配料、成型、干燥到能装窑 |
| `post_firing_hours` | 48 | 出窑到能发货 |
| `default_cycle_hours` | 48 | 窑炉没填周期时用（会提示） |
| `solver.time_limit_s` / `workers` | 10 / 8 | 求解时限、线程数 |
| `solver.weights` | 1000 / 10 / 50 / 1 / 5 | 排不下 / 逾期小时 / 开窑 / 完成时间 / 拆单 |
| `materials.*` | | 哪些批次状态算现存、在途，哪些采购单算在途 |
| `watch` | 5 分钟 / 24 小时 / planner | 新订单自动评估 |
| `shortage_notify_roles` | purchaser | 缺料推给谁 |

租户可在 `tenants/<id>/planning.yaml` 按段覆盖。

## 角色权限（luneng）

计划员：`pmc.*` 读写（确认计划、承诺交期、设置窑炉产能和产品烧成参数），仓储、采购单只读；
销售、销售负责人、采购：`pmc.kiln_plan:read`（看计划、做交期评估、看缺料）。

## 还没做（后续 PR）

- 换产清窑时间、窑炉保养窗口、班次日历；冻结期（已下发的近期窑次不再变）；
- 计划窑次与实际窑次（`kiln_run`）对照、计划达成率；
- 待现场确认：各窑装量和周期、烧成组划分、前后处理时间、配方用量（录入主数据即可，不改代码）。
