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
- `fos/services.py`：API 进程与信道进程共用的服务装配
- `crates/fos-protocol`：云边数据契约；`crates/fos-edge`：厂端 daemon

## 规则（不要破坏）

1. **租户隔离**：每张业务表有 `tenant_id`，每个查询按 `principal.tenant_id` 过滤。
2. **密钥不进代码、不进租户配置、不进模型上下文**；只走环境变量 / KMS。
3. **数字不让模型算**：用量、传热、金额、成本由确定性代码计算。
4. **模拟与真实不混用**：模拟结果带 `data_mode="mock"`，不能发布或导出正式文件。
5. **业务事实只通过 `Ledger.append()` 写入**：不要直接改 `objects` / `object_links`（它们是投影，可随时重建），`events` 表数据库层禁止改删。新业务 = 在本体 YAML 里加对象类型和事件类型，一般不需要写 Python。
6. **写操作只在人确认之后**：IM 消息是不可信输入，Agent 只提出卡片，确认时以点击人的权限执行；回复在事务提交后再发。
7. 厂端只执行白名单只读命令，协议里不存在写设备的命令。
8. 文档、注释、界面文案用中文；标识符用英文。

## 常用命令

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest -q                                   # SQLite + 模拟 LLM
FOS_TEST_DATABASE_URL=postgresql+asyncpg://... .venv/bin/pytest -q   # 真实 PostgreSQL（含并发测试）
.venv/bin/fos ontology luneng                         # 校验并查看合并后的本体
.venv/bin/ruff check . && .venv/bin/ruff format --check .
cargo fmt --all --check && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace
FOS_DATABASE_URL=postgresql+asyncpg://... .venv/bin/alembic upgrade head && .venv/bin/alembic check
```

## 节省额度

一个 PR 只做一件事；任务完成就开新会话；需要背景时读本文件和方案的对应章节，不做全仓搜索。
