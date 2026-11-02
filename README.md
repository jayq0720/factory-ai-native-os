# Factory AI Native OS

让没有任何信息化系统的工厂，跳过"先上 MES/ERP"，直接由常驻在线的工厂 Agent 承担录入、追溯、监控、分析和提醒。首个试点：六安绿能（隔热耐火材料）。

- 开发方案：`docs/development-plan.md`
- 演示页面：`demo/index.html`（全部模拟数据）
- 开发约定：`CLAUDE.md`

## 目录

| 路径 | 内容 |
|---|---|
| `fos/` | Python 核心 OS：配置、租户、权限、API、LLM 网关（之后加入本体、事件账本、Router/Worker…） |
| `crates/` | Rust：`fos-protocol` 云边共享数据契约，`fos-edge` 厂端 daemon |
| `tenants/<id>/tenant.yaml` | 租户配置（角色与权限、IM、自治级别），不含密钥 |
| `packs/` | 行业包与职能 Skill 包 |
| `migrations/` | 数据库迁移（Alembic） |
| `deploy/` | docker-compose 与 LiteLLM 配置 |

## 本地开发

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest                 # 测试用 SQLite + 模拟 LLM，不需要任何外部服务
.venv/bin/ruff check . && .venv/bin/ruff format --check .
cargo test --workspace           # Rust
```

## 用 docker-compose 起一套空系统

```bash
cp deploy/.env.example deploy/.env      # 填入百炼 / DeepSeek API Key（不提交）
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
docker compose -f deploy/docker-compose.yml exec api alembic upgrade head
docker compose -f deploy/docker-compose.yml exec api fos create-user luneng jayq JayQ --roles admin
docker compose -f deploy/docker-compose.yml exec api fos issue-key luneng jayq --label 本机调试
curl -H "Authorization: Bearer <上一步输出的 key>" http://localhost:8000/v1/me
```

默认 `FOS_LLM_MODE=mock`，不调用任何模型；把它改成 `litellm` 后经 LiteLLM 调用百炼上的 DeepSeek / Qwen，百炼故障时切 DeepSeek 官方 API。
