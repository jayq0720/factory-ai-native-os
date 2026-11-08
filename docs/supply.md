# 采购与仓储（W17，里程碑 M3 的第三段）

对应开发方案 5.3 “采购与供应商”“仓储物流”和 5.4 主干里的“缺料 → 采购员请购比价 → 到货 → 质检员检验 → 仓管员入库”：

- 库存不再是“到货批次合计”，而是**实时库存**：到货 − 领用 + 退料 ± 盘点，全部是账本事件的投影，可重放；
- 计划员确认排窑计划后有缺料，采购打开**请购建议**（缺口 + 安全库存 − 已请购），勾选生成**请购单**；
- **比价**选供应商，下**采购单**（金额由代码算，超阈值走审批）；仓管**按采购单登记到货**，批次待检，质检判定后才能领；
- 按来料合格率和准时率给**供应商打分**。

数字都由确定性代码算（CLAUDE.md 规则 3）；每个写操作都是人确认后以确认人身份 `Ledger.append()`（规则 5、6）。

## 一条完整的流程

```
主数据：物料 purchase.material_sourcing_set（安全库存、采购提前期、最小起订量、常用供应商；主数据导入“物料”表多了这几列）
        供应商报价 purchase.quote_recorded（拍照留证：单价、单位、交货周期、起订量、有效期）
                 │
POST /v1/planning/plans              计划员确认排窑计划 → 有缺料推给采购（W16）
GET  /v1/purchase/suggestions        请购建议（不入账）：max(计划缺口, 补到安全库存) − 未下单请购，不足起订量补到起订量；
                                     附建议供应商（比价推荐，没有报价时用常用供应商）
POST /v1/purchase/requisitions       采购员勾选确认 → purchase.requisition_raised（全有或全无）
GET  /v1/purchase/compare            比价：有效期内、单位一致、每家最新一张报价；能按需要日期到的里取最便宜，都赶不上取最快
POST /v1/purchase/orders             下采购单 purchase.order_created：金额 = 数量 × 单价（代码算）；引用请购单（被引用即“已下单”）；
                                     超过 policy.yaml 阈值（luneng 占位：> 5 万总经理审批）返回 202 进审批
POST /v1/purchase/orders/{id}/receipts  仓管按单登记到货 warehouse.lot_received（状态待检，附送货单照片）；到齐且有采购单权限时自动标为已到货
        │                            质检 quality.lot_dispositioned 判定合格 / 让步 / 不合格
POST /v1/stock/issues                领料 warehouse.stock_issued：指定批次，或只给物料按先进先出分配；领后低于安全库存推给仓管和采购
POST /v1/stock/returns               退料 warehouse.stock_returned
POST /v1/stock/counts/preview        盘点预览：盘点数 − 账面数
POST /v1/stock/counts                盘点确认 warehouse.stock_counted（盘盈 / 盘亏，附盘点照片）
GET  /v1/stock                       实时库存：每种物料的可用 / 待检 / 不可用、安全库存、批次（先进先出顺序、库位）
GET  /v1/purchase/requisitions       请购单（待下单 / 采购单审批中 / 已下单 / 已取消）；POST …/{id}/cancel 取消
GET  /v1/purchase/orders             采购单和到货进度（已到、未到、是否逾期）
GET  /v1/purchase/suppliers          供应商评分
```

网页：`/app/supply.html`（库存、领料、请购建议、请购单比价下单、采购单到货、供应商评分）。

IM（只读 Fast Path，零模型成本）：
- 「库存」「库存预警」：低于安全库存的物料（`warehouse.material_lot:read`）；
- 「库存 高铝矾土」：一种物料的可用、待检、不可用和批次；像一句话的（带“吗 / 多少 / ？”）交给 Agent；
- 「请购建议」：请购建议卡片（`purchase.purchase_requisition:read`）。

自然语言问“高铝矾土还够用几天、找谁买划算、甲厂靠不靠谱”走采购员 Skill（`packs/refractory/skills/purchaser`），
工具 `stock.query`、`purchase.suggestions`、`purchase.compare`、`purchase.supplier_scores`（只读）；
提问人明确要请购时提议 `purchase.requisition_raised` 卡片，采购员点确认才入账。

## 本体（fos/ontology/core.yaml，所有行业共用）

| 对象 | 说明 |
|---|---|
| `stock_move` 库存变动 | 批次、物料、类型（领用 / 退料 / 盘盈 / 盘亏）、数量（正数，方向由类型定）、单位、时间、生产批次、盘点单号 |
| `purchase_requisition` 请购单 | 请购单号、物料、数量、需要日期、来源（排窑计划号 / 安全库存）、原因、建议供应商、状态（待下单 / 已取消） |
| `supplier_quote` 供应商报价 | 供应商、物料、单价、单位、报价日期、有效期、交货周期、起订量 |
| 扩展 `material` | 采购提前期、最小起订量、常用供应商（安全库存原来就有） |
| 扩展 `material_lot` | 库位；`warehouse.lot_relocated` 移库 |
| 扩展 `purchase_order` | 采购单号、单价、请购单（多个）、依据报价、备注 |

“已下单”“已到货多少”不另记事件，从引用关系算：请购单被未取消的采购单引用 = 已下单；采购单的到货 = 填了这张采购单的来料批次合计。

## 账本守卫（fos/supply/guards.py）

不论从专用接口、`/v1/events`、卡片确认还是审批通过后执行，都会再查一次：

- **领料**：只能领 `usable_statuses`（合格、让步接收）的批次——**待检、不合格不能领**（红线）；不能超过现存；
- **退料**：不能超过这个批次已领未退的量；**盘亏**：不能超过账面现存；
- 类型要和事件一致、数量为正、批次和物料一致、单位一致（`orders.yaml` 的同义词归一）；
- **按采购单到货**：物料、供应商、单位要和采购单一致，采购单没关闭 / 取消，累计到货 ≤ 订量 × (1 + `over_receive_pct`)。
  填的采购单号在系统里找不到（纸质老单号）时放行。

## 配置（packs/refractory/supply.yaml，全部是占位值）

| 项 | 默认 | 说明 |
|---|---|---|
| `stock.usable_statuses` | accepted、concession | 能领、算作现存的批次状态 |
| `stock.incoming_statuses` | pending_inspection | 待检：不能领，但请购建议里算作马上能用 |
| `stock.low_stock_notify_roles` | warehouse、purchaser | 领料后低于安全库存推给谁 |
| `receiving.over_receive_pct` | 5 | 超订容差 |
| `requisition.round_to_min_qty` | true | 请购建议不足起订量时补到起订量 |
| `suppliers.window_days` / `weights` | 180 / 质量 60、准时 40 | 供应商评分 |

租户可在 `tenants/<id>/supply.yaml` 按段覆盖。采购单金额审批阈值在 `policy.yaml`（不在这里）。

## 供应商评分口径

最近 `window_days` 天到货的批次：合格率按合格 1、让步 0.5、不合格 0（待检不计）；准时率 = 到货日 ≤ 采购单交期的批次占比
（没关联采购单或采购单没交期的不计）；总分 = 两项按权重加权平均 × 100，只算有数据的项，都没数据不打分。

## 角色权限（luneng）

采购：`purchase.*` 读写（请购、报价、采购单、物料采购参数），仓储只读；仓管：`warehouse.*` 读写（到货、领料、退料、盘点、移库），
采购单和供应商只读；计划员：请购单只读；总经理：全部只读 + 审批。

## 还没做（后续 PR）

- 报价单 / 送货单拍照自动识别成报价、到货草稿（录单 Schema `entry_*.yaml` 加映射即可）；拍照盘点识别；
- 三单匹配里的发票（财务 M4）；付款审批；
- 成品库存的库位、装箱单、托盘码；扫码出入库；
- 待现场确认：安全库存、提前期、起订量（主数据）、超订容差、评分权重（配置）。
