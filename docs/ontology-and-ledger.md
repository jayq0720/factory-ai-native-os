# 企业本体与事件账本

对应开发方案 5.1（原则 1、2、7）和 5.2。这份说明写给要加新业务的人。

## 三句话

1. 全公司只有一张对象图（客户、订单、原料批、窑次、批次、检验……），各职能只是看它的不同角度。
2. 发生的每件事都记成一条**事件**：谁、什么时候、凭什么证据、改了哪个对象。事件只追加，数据库层禁止修改和删除。
3. “当前状态”和“引用图”是事件的**投影**，随时可以从事件完整重放出来（`fos rebuild <租户>`）。

## 本体放在哪

| 层 | 文件 | 谁改 |
|---|---|---|
| 核心 | `fos/ontology/core.yaml` | 所有行业通用：客户、供应商、物料、原料批、产品、订单、生产批次、检验、设备 |
| 行业包 | `packs/refractory/ontology.yaml` | 耐火材料：窑炉、窑次、配方；给产品、批次、检验追加行业属性 |
| 租户 | `tenants/<id>/ontology.yaml`（可选） | 本厂特有的对象或属性 |

后面的层只能**新增**对象、事件，或给已有对象**追加非必填属性**；不能改已有属性，避免把上层的数据和 Skill 弄坏。启动时会合并并整体校验，不合法就拒绝启动。

## 加一种新业务的步骤

例：加“设备报修”。

```yaml
# packs/refractory/ontology.yaml 或 tenants/luneng/ontology.yaml
objects:
  repair_ticket:
    name: 报修单
    domain: eam            # 读权限 = eam.repair_ticket:read
    properties:
      equipment: {type: ref, target: equipment, name: 设备, required: true}
      symptom: {type: text, name: 故障现象, required: true}
      status: {type: enum, name: 状态, values: [open, assigned, fixed, closed]}
events:
  eam.repair_reported:
    name: 报修
    object: repair_ticket
    op: create
    permission: eam.repair:write
    evidence: required     # 现场照片
    fields: [equipment, symptom, status]
  eam.repair_closed:
    name: 修复关闭
    object: repair_ticket
    op: update
    permission: eam.repair:approve
    fields: [status]
```

然后在 `tenants/luneng/tenant.yaml` 里给相应角色加权限，运行 `fos ontology luneng` 检查。**不需要写 Python、不需要数据库迁移**：投影和追溯会自动支持新对象。

## 一条事件

```json
POST /v1/events
{
  "type": "warehouse.lot_received",
  "object_id": "L-KF1019",
  "payload": {"material": "M-AL88", "supplier": "S-KY", "qty": 32.46, "unit": "t",
              "received_at": "2026-11-02T08:30:00+08:00", "status": "pending_inspection"},
  "evidence": [{"kind": "photo", "ref": "oss://luneng/im/2026-11-02/0001.jpg", "note": "送货单"}],
  "idempotency_key": "dingtalk:msg-889",
  "correlation_id": "thread-0412"
}
```

账本依次检查：事件类型存在 → 调用者有该事件的权限 → 需要证据的必须带证据 → 幂等键是否已用过（重复提交直接返回原结果）→ 字段属于该事件、类型正确、必填齐全 → 引用的对象存在于同一租户 → 模拟数据与真实数据不混用 → 乐观并发版本（可选）。全部通过才分配租户内序号、写事件、更新投影，在一个事务里完成。

## 读

| 接口 | 用途 |
|---|---|
| `GET /v1/ontology` | 合并后的本体 |
| `GET /v1/events?after_seq=` | 按序号读事件流（Agent 订阅、同步用）；只返回有读权限的对象类型 |
| `GET /v1/objects/{type}/{id}` | 当前状态 + 完整事件历史 |
| `GET /v1/objects/{type}/{id}/trace?depth=4` | 链式追溯：沿引用双向展开，例如批次 → 原料批 → 供应商 / 来料检验、批次 → 窑次 → 窑炉、批次 ← 出厂检验；无权限的节点保留位置、隐去内容 |
