# 财务集成与成本核算（W23）

对应方案 5.5（财务总账、税务申报、开票集成金蝶/用友/畅捷通；成本计算由确定性代码完成，结果可复算）与 10.3（W23）。

## 设计原则

1. **账本唯一真相**：应收单（`receivable`）、应付单（`payable`）、凭证草稿（`voucher_draft`）全部以事件账本 + 对象投影实现，不建冗余财务总账表。
2. **账本守卫兜底**（`fos/finance/guards.py`）：
   - 严禁超收（累计回款不能超过应收金额）、严禁超付（累计付款不能超过应付金额）；
   - 凭证草稿强制复式记账校验：至少 2 行分录、金额非负有限、借方合计必须精确等于贷方合计。
3. **大额付款审批**（`tenants/luneng/policy.yaml`）：单笔供应商付款 `≥ 50,000` 元自动触发总经理（`gm`）审批，返回 `202`，审批通过后入账。
4. **确定性成本核算**（`fos/finance/costing.py`）：按本批次净领料（领料 - 退料）× 最新采购单价 + 窑次能耗（电/燃气，按同窑次产量占比分摊）+ 人工与制造费用定额计算总成本与单位成本，缺项显式返回 `warnings`。
5. **外部财务软件对接与导出边界**：
   - `POST /v1/finance/vouchers/from-source` 按应收、回款、应付、付款单据确定性价税分离并生成凭证草稿；
   - `GET /v1/finance/vouchers/export?target=kingdee|yonyou|generic` 导出金蝶/用友/通用 Excel 凭证导入表，严禁模拟数据导出；
   - `GET /v1/finance/reconciliation/{customer|supplier}/{id}/export` 导出正式往来对账单，同样禁止模拟数据。

## 接口与入口

- REST API：`/v1/finance/{ar,ap,cost/batches/{id},cost/orders/{id},vouchers,vouchers/from-source,vouchers/export,reconciliation/{party_type}/{party_id},remind}`
- Web 控制台：`/app/finance.html`
- IM Fast Path：`应收` / `回款`、`应付` / `付款`、`成本 <批号>`、`凭证`
- 只读 Agent 工具与 Skill：`finance_assistant`（`finance.ar_summary`、`finance.ap_summary`、`finance.batch_cost`、`finance.vouchers`）
