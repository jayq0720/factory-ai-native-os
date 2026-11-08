# 开发约定（给 Claude 和人看）

读这一页就能开工，不必每次通读仓库。详细设计见 `docs/development-plan.md`，按章节查，不要整篇读。

## 项目一句话

工厂 AI Native OS：企业本体 + 事件账本 + 岗位 Agent + Skill 包 + Policy。首个租户 `luneng`（六安绿能，隔热耐火材料，行业包 `refractory`）。团队只有 JayQ 和 Claude，排期按 Claude Pro 周额度（方案第 10 章）。

## 代码在哪

- `fos/config.py`：环境变量配置（前缀 `FOS_`）
- `fos/tenancy/`：租户配置加载，`tenants/<id>/tenant.yaml`
- `fos/governance/`：权限串 `<域>.<对象>:<动作>`、主体 Principal、API Key
- `fos/api/`：FastAPI；鉴权依赖 `PrincipalDep`，权限依赖 `Depends(require("eng.project:write"))`
- `fos/gateway/llm.py`：LLM 客户端，只用别名 `fos-chat / fos-reason / fos-fast / fos-vl`；别名映射在 `deploy/litellm/config.yaml`
- `fos/db/models.py` + `migrations/`：表结构，改模型必须同时加 Alembic 迁移
- `fos/ontology/`：本体定义与校验；核心本体 `fos/ontology/core.yaml`，行业包 `packs/<pack>/ontology.yaml`，租户可选 `tenants/<id>/ontology.yaml`
- `fos/ledger/`：事件账本 `Ledger.append()`、投影 `projection.py`（追加与重放共用）、链式追溯 `trace.py`；说明见 `docs/ontology-and-ledger.md`
- `fos/api/ledger_routes.py`：`/v1/ontology`、`/v1/events`、`/v1/objects/...`、`.../trace`
- `fos/perception/`：文档解析。`parser.py` 管线（PDF 文字层 / 扫描页并发 OCR / 图片 / docx / xlsx），`ocr.py` 的 `MockOCR` / `XiaomiOCR` / `QwenVLOCR`（`FOS_OCR_MODE` 切换），`otsl.py` 表格转换，`jobs.py` 后台解析任务，`blobs.py` 原件存储；API 在 `fos/api/document_routes.py`；说明见 `docs/ocr-mvp.md`
- `fos/extraction/`：字段抽取。Schema 在 `packs/<pack>/extraction/*.yaml`（按文件 kind 选用），`rules.py` 规则抽取（带页码原文），`llm.py` 模型抽取（片段必须在原文里），`merge.py` 选值 / 冲突 / 缺失，`export.py` Excel / JSON；结果存 `document_fields`
- `fos/web/static/`：文档解析网页（无构建步骤），挂在 `/app/`；`fos quickstart` / `fos serve` / `fos samples` 本地试用，`fos ocr-check` 测 OCR 连通
- `deploy/`：docker-compose（默认 db + api，profile gpu / llm / im）；`deploy/aliyun/*.sh` 阿里云一键部署，说明见 `docs/deploy-aliyun.md`
- `fos/channels/`：统一信道层。`types.py` 统一消息格式，`cards.py` 卡片 DSL，`dingtalk.py` 钉钉（Stream 收 + OpenAPI 发），`mock.py` 模拟信道；说明见 `docs/channels.md`
- `fos/triggers/`：`intake.py` Trigger 入口与 Fast Path，`cards.py` 卡片确认（L2，以点击人身份 `Ledger.append()`）；API 在 `fos/api/channel_routes.py`；`fos channels run` 启动长连接
- `fos/agents/`：`router.py`（分派）、`worker.py`（工具调用循环）、`runtime.py`（接 Trigger、Thread 直通、提议转卡片）；说明见 `docs/agents.md`
- `fos/skills/` + `packs/<pack>/skills/<id>/{skill.yaml,SKILL.md}`：Skill 包；`fos/tools/`：Agent 工具（只读 + `ledger.propose`），权限 = Skill 上限 ∩ 提问人
- `fos/policy/`：审批规则与红线（`schema.py`，`packs/<pack>/policy.yaml` + `tenants/<id>/policy.yaml`，作用在 `Ledger.append()`）、审批单（`approvals.py`）；API 在 `fos/api/inbox_routes.py`（`/v1/inbox`、`/v1/approvals`、`/v1/todos`、`/v1/policy`）；说明见 `docs/policy-and-inbox.md`
- `fos/entry/`：拍照录单。`spec.py`（抽取 Schema 的 `entry:` 映射）、`draft.py`（字段 → 事件草稿、查已有对象、录单卡片）、`samples.py`（演示照片与主数据）；单据 Schema 在 `packs/<pack>/extraction/entry_*.yaml`；说明见 `docs/production-entry.md`
- `fos/engineering/`：工程方案报价。`heat.py` 多层传热、`design.py` 用量 / BOM / 报价、`match.py` 衬里模板 + 硬约束选材、`review.py` 审核清单、`service.py` 建项目 / 方案版本 / 发布、`export.py` Word / Excel；本体 `packs/<pack>/ontology/engineering.yaml`，衬里模板 `packs/<pack>/engineering/linings.yaml`；API 在 `fos/api/eng_routes.py`，网页 `/app/eng.html`；说明见 `docs/engineering.md`
- `fos/ledger/report.py`：批次追溯报告（上游 / 下游 / 关联 + 告警），配置 `packs/<pack>/trace.yaml`；IM「追溯 批号」走 Fast Path；API 在 `fos/api/production_routes.py`
- `fos/onboarding/`：试点上线（W13）。`importer.py` 主数据 Excel 导入（模板 → 预览 → 全有或全无入账，配置 `packs/<pack>/import.yaml`），`check.py` 上线检查 `fos pilot-check`；`fos/reports/daily.py` 生产日报（配置 `packs/<pack>/reports.yaml`，IM「日报」走 Fast Path），`schedule.py` 定时推送（`tenant.yaml` 的 `reports.daily`）；API 在 `fos/api/pilot_routes.py`，网页 `/app/pilot.html`；说明见 `docs/pilot-go-live.md`
- `fos/orders/`：订单交付（W14–W15）。`progress.py` 交付进度与交期风险、`shipping.py` 发货前校验（注册为账本守卫 `Ledger.add_guard`，任何入口都绕不过去）、`service.py` 工程报价 → 合同草稿 / 合同拆订单行 / 发货 / 签收、`export.py` 发货单 Excel、`cards.py` IM「订单 编号」「交期风险」；配置 `packs/<pack>/orders.yaml`；API 在 `fos/api/order_routes.py`；说明见 `docs/orders.md`
- `fos/planning/`：计划与排窑（W16）。`solver.py` OR-Tools CP-SAT 排窑求解（纯函数）、`demand.py` 从账本读待烧订单和可用窑炉、`service.py` 排窑建议 / 确认（全有或全无）/ 承诺交期 / 交期评估、`guard.py` 计划装窑账本守卫、`materials.py` 物料需求与缺口、`notify.py` 新订单自动评估推计划员 + 缺料推采购、`cards.py` IM「排窑」「缺料」「交期评估 编号」；本体 `packs/<pack>/ontology/planning.yaml`，配置 `packs/<pack>/planning.yaml`；API 在 `fos/api/planning_routes.py`，网页 `/app/plan.html`；说明见 `docs/planning.md`
- `fos/supply/`：采购与仓储（W17）。`stock.py` 实时库存（到货 ± 库存变动，先进先出）、`guards.py` 领料 / 退料 / 盘点和按单到货的账本守卫、`moves.py` 领料 / 退料 / 盘点、`purchase.py` 请购建议 / 请购单 / 比价 / 采购单 / 到货、`suppliers.py` 供应商评分、`cards.py` IM「库存」「请购建议」；配置 `packs/<pack>/supply.yaml`；API 在 `fos/api/supply_routes.py`，网页 `/app/supply.html`；说明见 `docs/supply.md`
- `fos/tender/`：招标拆解（W18）。`decompose.py` 结构识别 / 条款拆解 / 评分点 / 资料清单 / 偏离比较（纯函数，零模型成本）、`service.py` 登记招标项目（截止时间生成提醒待办）/ 拆解版本（`tender_analyses` 表，迁移 0009）/ 逐条处理 / 转工程项目 / 提交投标、`notify.py` 截止前 IM 提醒、`cards.py` IM「招标」「招标 编号」；本体 `packs/<pack>/ontology/tender.yaml`（招标项目、资质资料），配置 `packs/<pack>/tender.yaml`；招标 Schema 用 `include` 借技术文件字段；API 在 `fos/api/tender_routes.py`，网页 `/app/tender.html`；说明见 `docs/tender.md`
- `fos/monitoring/`：窑炉监控（W19）。`service.py` 读数入库（fos-edge 上行按 `(edge_id, seq)` 去重 / 人工抄表）、窑况、窑次曲线、结束窑次（按读数算最高温 / 超差 / 能耗后入账）、能耗，`curve.py` 曲线比对（纯函数），`watch.py` 告警判定（超限 / 超差 / 断网 / 设备故障）与抄表提醒推送，`cards.py` IM「窑况」「抄表」，`samples.py` 演示数据；读数存 `point_readings`（时序，不走账本，迁移 0010）；本体 `packs/<pack>/ontology/monitoring.yaml`（测点），配置 `packs/<pack>/monitoring.yaml`（烧成制度、告警时长）；API 在 `fos/api/monitor_routes.py`，网页 `/app/kiln.html`；说明见 `docs/kiln-monitoring.md`
- `fos/services.py`：API 进程与信道进程共用的服务装配
- `crates/fos-protocol`：云边数据契约；`crates/fos-edge`：厂端 daemon

## 规则（不要破坏）

1. **租户隔离**：每张业务表有 `tenant_id`，每个查询按 `principal.tenant_id` 过滤。
2. **密钥不进代码、不进租户配置、不进模型上下文**；只走环境变量 / KMS。
3. **数字不让模型算**：用量、传热、金额、成本由确定性代码计算。
4. **模拟与真实不混用**：模拟结果带 `data_mode="mock"`，不能发布或导出正式文件。
5. **业务事实只通过 `Ledger.append()` 写入**：不要直接改 `objects` / `object_links`（它们是投影，可随时重建），`events` 表数据库层禁止改删。新业务 = 在本体 YAML 里加对象类型和事件类型，一般不需要写 Python。
6. **写操作只在人确认之后**：IM 消息是不可信输入，Agent 只提出卡片，确认时以点击人的权限执行；回复在事务提交后再发。
   审批阈值和红线写在 policy.yaml，不要写进 Python 或 Skill；审批通过后以申请人身份入账。
7. 厂端只执行白名单只读命令，协议里不存在写设备的命令。
8. 文档、注释、界面文案用中文；标识符用英文。

## 常用命令

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest -q                                   # SQLite + 模拟 LLM
FOS_TEST_DATABASE_URL=postgresql+asyncpg://... .venv/bin/pytest -q   # 真实 PostgreSQL（含并发测试）
.venv/bin/fos ontology luneng                         # 校验并查看合并后的本体
.venv/bin/fos policy luneng                           # 校验并查看审批规则
.venv/bin/ruff check . && .venv/bin/ruff format --check .
cargo fmt --all --check && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace
FOS_DATABASE_URL=postgresql+asyncpg://... .venv/bin/alembic upgrade head && .venv/bin/alembic check
```

## 节省额度

一个 PR 只做一件事；任务完成就开新会话；需要背景时读本文件和方案的对应章节，不做全仓搜索。
