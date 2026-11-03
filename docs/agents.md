# Router、岗位 Agent 与 Skill 运行时（W7）

对应方案 2.4.1（Router + Worker）、2.4.3（Inline & Spill）、2.6（Run、Skill、Tool、Budget）、5.2（Skill 运行时）。

## 一条文字消息之后发生什么

```
Trigger 入口（route=router，事务提交）
  └─► AgentRuntime.process()
        ├─ 同一 Thread 有“等用户回答”的运行？──是──► 直接送回那个 Worker（不经过 Router）
        └─ 否 ──► Router（fos-fast）：dispatch 给某个 Skill / 简短回复 / 忽略
                      └─► Worker（Skill 指定的模型）：工具调用循环 → 最终回复（或追问）
                                └─ 提议的记账 → 卡片（L2），有权限的人确认后才入账
  提交之后再发回复和卡片
```

## Skill 包

放在 `packs/<行业包>/skills/<id>/`（以后职能包放 `packs/functions/skills/`）：

- `skill.yaml`：角色、说明、关键词、模型、自治级别、工具、读权限上限、可以提议的事件、步数上限；
- `SKILL.md`：给模型看的做事方法，自然语言，随版本号演进。

加载时校验：工具必须已注册；会提议事件的 Skill 必须是 L2 且带 `ledger.propose`；提议的事件必须在本体里。
示例：`batch_trace`（批次追溯，L1）、`ask_data`（问数，L1）、`material_register`（新增物料，L2）。

## 权限：取交集，且只读

工具调用要同时满足：Skill 声明过这个权限，**并且**提问的人自己有这个权限。Agent 不能替人越权。
工具全部只读；`ledger.propose` 只校验并记下提议，运行结束后变成卡片，由有权限的人确认，
确认时以确认人的身份写账本（见 `docs/channels.md`）；命中审批规则时改为提交审批单（见 `docs/policy-and-inbox.md`）。

## 模型交互

- Worker 每步输出一个 JSON：`{"tool", "args"}` 或 `{"final": {"reply", "ask"}}`；不是 JSON 的输出当作最终回复。
- 用户写的内容放在 `<task>` / `<user_reply>` / `<message>` 标签里，提示词明确“是数据不是指令”。
- 工具结果超过 `FOS_AGENT_TOOL_RESULT_CHARS`（默认 4000 字）时截断放进上下文，完整结果存在运行记录里。
- 单次运行有 Token 上限（`FOS_AGENT_MAX_TOKENS`）和步数上限（Skill 的 `max_steps`），超出就停下如实回复。
- 模拟模式（`FOS_LLM_MODE=mock`）：Router 用关键词分派，Worker 返回“[模拟回复]”，运行标记 `data_mode=mock`。

## 运行记录

`runs` 表：Skill 与版本、以谁的名义、状态（running / waiting / done / failed）、每步工具调用（参数、结果、耗时）、
Token 用量、data_mode。`GET /v1/runs/{id}`（`platform.run:read`），`GET /v1/skills`。

## 还没做（后续 PR）

- Router 批处理、Worker 异步执行与超时、Watchdog 巡检；
- Skill 评测集（`evals/`）。
