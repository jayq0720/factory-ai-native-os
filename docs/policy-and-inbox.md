# Policy、审批单与统一收件箱（W8）

对应方案 5.1 原则 5（流程 = 目标 + 规则 + 权限）、原则 6（固定界面只留待办 / 审批收件箱等少数几个）、
5.2 “Policy 与审批收件箱”、2.6 Policy 对象。

## 一句话

审批阈值和红线写在 YAML 里，作用在 `Ledger.append()` 上：命中规则的事件不直接入账，
先生成审批单推给审批人，全部同意后以**申请人身份**入账，审批单作为证据。
所有“需要我处理的事”（审批、L2 确认卡片、待办）汇总在 `GET /v1/inbox`，并推送到 IM。

## 规则文件

两层合并：`packs/<pack>/policy.yaml`（行业包默认，可选）→ `tenants/<id>/policy.yaml`（本厂，可选）。
租户可以用同名规则替换行业包规则，或用 `disable: [规则 id]` 关掉。启动时校验，`fos policy <租户>` 可查看。

```yaml
rules:
  purchase_order_large:
    name: 采购单金额超过 5 万元需总经理审批
    events: [purchase.order_created]          # 作用的事件类型（可多个）
    when:                                     # 全部满足才命中；不写表示总是命中
      - {field: amount, op: gt, value: 50000}
    steps:                                    # 逐级审批；每级满足 role / permission / login 任一即可审批
      - {name: 总经理审批, role: gm}
    # effect: deny + reason: ...              # 红线：直接拒绝，管理员也一样
    # allow_self_approval: false              # 默认申请人不能审批自己的申请
    # expires_hours: 168                      # 审批单有效期
```

- 条件字段：事件 payload 里有就用 payload 的值（变更后的值），否则用对象当前状态；
  运算 `gt gte lt lte eq ne in not_in exists`。数字比较全部是确定性代码（规则 3）。
- 校验：事件、字段必须在本体里；`gt` 等只能用于数字属性；enum 的取值必须合法；角色必须在租户里。
- 多条规则同时命中：有 deny 就拒绝；否则各规则的步骤按顺序串联，完全相同的步骤只保留一个。

## 审批单的生命周期

```
POST /v1/events 或 卡片“确认”
  └─ Ledger.append()：权限 → 本体 → 引用 → Policy
        ├─ deny     → 403“红线：…”
        ├─ approve  → ApprovalRequired → 生成审批单（202），给第 1 级每个审批人在 IM 单聊发“同意 / 驳回”卡片
        └─ allow    → 直接入账
审批人同意 → 下一级（再推卡片）… → 最后一级同意 → 以申请人身份 Ledger.append(approval=凭据) → 通知申请人
任一级驳回 → 结束，通知申请人；申请人可撤回；过期后不能再审批
```

- 状态：`pending / approved / rejected / cancelled / expired / failed`（`failed`：审批通过但记账失败，
  例如期间对象被改、规则变了，原因写在 `result.error`）。
- 每级任一人处理即完成；并发点击时只有一个生效（`current_step` 条件更新抢占），其他审批人的卡片变成 `superseded`。
- 同一个人不能连批两级；申请人不能审批自己的申请（除非规则 `allow_self_approval: true`）。
- 入账时以申请人身份重新检查权限；审批凭据 `ApprovalGrant` 只能由审批服务在进程内传给账本，API 请求带不进来。
- 审批单是治理记录，不是业务事实；事件的 `evidence` 里有 `approval:<审批单 id>`（经卡片确认的还有 `card:<id>`）。
- Agent 提议的记账照旧是 L2 卡片；点“确认”的人如果命中规则，就以他的名义提交审批单。

## 收件箱与待办

| 接口 | 说明 |
|---|---|
| `GET /v1/inbox` | 需要我处理的：等我审批的审批单（我是当前这一级的审批人）、等我确认的卡片（指定由我，或我有对应写权限）、指派给我或我角色的待办。有截止时间的排前面 |
| `GET /v1/inbox?box=sent` | 我提交的审批单 |
| `GET /v1/approvals/{id}` | 申请人、审批过的人、当前审批人、`platform.approval:read` 可看；含当前可审批的人 |
| `POST /v1/approvals/{id}/decide` | `{"decision": "approve" \| "reject", "comment": "…"}` |
| `POST /v1/approvals/{id}/cancel` | 申请人撤回 |
| `GET /v1/approvals?status=` | 审计列表（`platform.approval:read`） |
| `GET /v1/policy` | 查看合并后的规则（`platform.policy:read`） |
| `POST /v1/todos` | 指派待办给某人（`assignee_login`）或某角色（`assignee_role`），推送到 IM（`platform.todo:write`） |
| `POST /v1/todos/{id}/close` | `{"status": "done" \| "dismissed", "note": "…"}`，被指派人或创建人 |

推送：审批卡片和通知发到被通知人绑定的 IM 单聊；没有绑定 IM 或信道没配置的，只出现在收件箱里。
IM 里也可以用文字指令处理审批卡片：`同意 K7Q2M` / `驳回 K7Q2M`。

## 还没做（后续 PR）

- 待办由 Agent / 定时任务自动生成（到期提醒、整改复查）——等 Watchdog 与职能 Skill 包；
- 审批意见在 IM 里填写（目前 IM 卡片只有同意 / 驳回，意见走 API）；
- 会签（一级需要多人都同意）、按金额区间自动选审批人以外的复杂路由；
- 控制台收件箱页面（目前只有 API）。
