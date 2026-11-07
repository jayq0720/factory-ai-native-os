# 订单交付（W14–W15，里程碑 M3 的第一段）

对应开发方案 5.4 “订单到回款主干”的前半段：工程报价 → 合同 → 订单行 → 生产批次 → 入库 → 出厂检验 → 发货 → 签收。
计划排窑、采购、仓储、招标拆解在后续 PR（W16–W18）。

## 一条完整的流程

```
GET  /v1/contracts/draft?eng_project=EP-…   已发布的工程报价 → 合同草稿 + 订单行（只是草稿，人核对后提交）
POST /v1/contracts                          sales.contract_signed（或 sales.contract_signed_from_quote，关联工程项目）
        │                                   合同 ≥ 50 万 → 审批（销售负责人 → 总经理），通过后以申请人身份入账
        │                                   有工程写权限的人登记时，工程项目自动标为赢单
POST /v1/contracts/{id}/orders              合同拆订单行：每行一个 sales.order_created，全有或全无，重复提交幂等
        ▼
production.batch_started（order = 订单）→ batch_completed → warehouse.batch_stocked → 出厂检验（kind = final）
        ▼
POST /v1/orders/{id}/shipments/check        发货预检：阻断问题 + 提示
POST /v1/shipments                          warehouse.shipment_dispatched（账本守卫再查一次）
GET  /v1/shipments/{id}/export              发货单 Excel（批号、检验报告编号来自账本）
POST /v1/shipments/{id}/receipt             sales.shipment_received：签收（签收数量）或拒收（必须写原因）
        ▼
GET  /v1/orders、/v1/orders/{id}            订单看板 / 单个订单进度
```

IM（只读 Fast Path，零模型成本）：
- 「订单 HT2026-018-1」：订单进度卡片（订单编号、订单号都可以；发合同编号回合同下全部订单行）；
- 「交期风险」：只列有高风险的订单；「订单看板」「在手订单」：全部在手订单。

## 本体

| 对象 | 说明 |
|---|---|
| `contract` 销售合同 | 合同编号、客户、签订日期、金额、交期、付款方式、质保、交货地点；行业包追加 `eng_project`、`quote_no` |
| `sales_order` 订单 | 一个产品一行；新增 `order_no`、`contract`、`unit_price`、`note` |
| `shipment` 发货 | 订单、发货批次（多个）、数量、发货时间、承运方、车牌；签收人、签收时间、签收数量；状态 在途 / 已签收 / 客户拒收 |

| 事件 | 权限 | 证据 |
|---|---|---|
| `sales.contract_signed` / `sales.contract_signed_from_quote` | `sales.contract:write` | 合同扫描件 |
| `sales.contract_status_changed` | `sales.contract:write` | |
| `sales.order_rescheduled` 交期变更 | `sales.order:write` | 客户确认 |
| `warehouse.shipment_dispatched` 发货出库 | `warehouse.shipment:write` | 发货单 / 装车照片或仓管确认 |
| `sales.shipment_received` 签收回执 | `sales.delivery:write` | 签收单照片 |

订单的“交付进度”不是一个字段，而是从关联对象算出来的（不会出现“状态写着已发货、其实没发”）；`status` 只用来关闭、取消。

## 谁算数字（fos/orders/progress.py，确定性代码）

| 数量 | 算法 |
|---|---|
| 排产 | 本订单未报废批次的计划数量（没有计划数量的已完工批次按合格数量） |
| 完工 / 入库 | 已完工 / 已入库批次的合格数量 |
| 发货 | 未拒收的发货数量；签收 = 签收数量（没填按发货数量） |
| 待发 | 订单数量 − 发货 |

单位按 `orders.yaml` 的 `unit_aliases` 归一（吨 = t），**不做换算**；单位不同的批次 / 发货不计入，并提示。

进度阶段：待排产 → 已排产 → 生产中 → 待发货 → 部分发货 → 已发货 → 已签收（另有已关闭、已取消）。

交期风险（`orders.yaml` 的 `risk`，占位值）：已逾期；距交期 7 天内排产量不够；距交期 2 天内可发库存不够；
没有交期；已入库批次出厂检验不合格 / 没有检验；客户拒收；单位不一致。

## 发货前校验：账本守卫

`policy.yaml` 的规则只看得到被写的那一个对象；发货要同时看订单、批次、检验、已发数量，所以做成**账本守卫**
（`Ledger.add_guard`，在本体校验、引用检查之后、Policy 之前执行）。不论从发货接口、`/v1/events`、卡片确认还是审批通过后执行，
都会再查一次，有阻断问题就拒绝写入。规则参数在 `packs/<pack>/orders.yaml` 的 `shipping`：

| 规则 | 默认（占位） | 不满足时 |
|---|---|---|
| 只能发已入库的批次 | 是 | 阻断 |
| 每个批次要有出厂检验且合格 | 是 | 阻断（关掉后变提示） |
| 有条件放行的批次 | 可以发 | 提示 |
| 别的订单排产的批次 | 不能发 | 阻断（打开后变提示）；同产品的无主库存可以 |
| 累计发货 ≤ 订单数量 × (1 + 3%) | 3% | 阻断 |
| 本次数量 ≤ 批次可发数量 | — | 阻断（可发 = 合格数量 − 引用这些批次的未拒收发货；多批次合发整单计入，偏保守） |
| 批次产品与订单一致、订单未关闭 / 取消、单位一致 | — | 阻断 |
| 没填批次 | — | 提示（客户异议时追溯不到） |

## 审批（tenants/luneng/policy.yaml，阈值为占位值）

- 合同金额 ≥ 50 万：销售负责人 → 总经理（合同评审）；合同拆出的订单行不再重复审批；
- 没有合同的散单 ≥ 50 万：同上（原规则，加了“合同未填写”条件）；
- 交期变更：销售负责人确认。

## 销售助理 Skill（packs/refractory/skills/order_desk）

工具 `orders.progress`、`orders.board`（只读，数字由代码算，模型直接引用），可以提议 `sales.order_rescheduled`
（客户在聊天里确认新交期时，以这条消息为证据，生成卡片由销售确认，再走审批）。

## 角色权限（luneng）

销售、销售负责人加了批次、出厂检验、发货的读权限（看订单进度明细）；仓管加了订单、客户、产品、合同的读权限（发货要看）；
计划员加了订单读权限。看不到明细的人只看到汇总数量。

## 还没做（后续 PR）

- 合同扫描件 OCR 抽取（金额、交期、付款、质保条款）并与报价比对；
- 质保书（按批次检验结果生成）——放在质量体系（W19–W22）；
- 开票草稿、按付款节点追回款——财务（W23–W24，M4）；
- 交期承诺按产能评估、排窑——计划与排窑（W16）；
- 发货单拍照录单（目前发货由系统生成单据，仓管确认）；控制台网页。
