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
- `crates/fos-protocol`：云边数据契约；`crates/fos-edge`：厂端 daemon

## 规则（不要破坏）

1. **租户隔离**：每张业务表有 `tenant_id`，每个查询按 `principal.tenant_id` 过滤。
2. **密钥不进代码、不进租户配置、不进模型上下文**；只走环境变量 / KMS。
3. **数字不让模型算**：用量、传热、金额、成本由确定性代码计算。
4. **模拟与真实不混用**：模拟结果带 `data_mode="mock"`，不能发布或导出正式文件。
5. **业务事实只通过 `Ledger.append()` 写入**：不要直接改 `objects` / `object_links`（它们是投影，可随时重建），`events` 表数据库层禁止改删。新业务 = 在本体 YAML 里加对象类型和事件类型，一般不需要写 Python。
6. 厂端只执行白名单只读命令，协议里不存在写设备的命令。
7. 文档、注释、界面文案用中文；标识符用英文。

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
