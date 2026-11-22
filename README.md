# Factory AI Native OS

让没有任何信息化系统的工厂，跳过"先上 MES/ERP"，直接由常驻在线的工厂 Agent 承担录入、追溯、监控、分析、审批与提醒。首个试点与行业包：**六安绿能（隔热耐火材料 `packs/refractory`）**，已支持多租户零代码复制至同行业其他工厂。

- 架构与 31 周路线图：`docs/development-plan.md`（**M1–M5 全职能里程碑已 100% 完成**）
- 工程规范与代码地图：`CLAUDE.md`
- 演示页面：`demo/index.html`（模拟数据演示）

## 核心能力全景（M1–M5 全职能闭环）

- **底层 OS 与治理**：三层企业本体（核心/行业包/租户）、不可变事件账本（`Ledger.append()` + 证据链）、RBAC 权限与审批红线引擎、三端 IM 适配器（钉钉 Stream / 飞书 / 企业微信）、Fast Path + Router/Worker 分层调度与多租户隔离。
- **生产与现场**：拍照录单与批次双向追溯（M1，`docs/production-entry.md`）、Rust 厂端网关 `fos-edge` + 窑炉曲线监控与能耗分析（`docs/fos-edge.md`、`docs/kiln-monitoring.md`）。
- **工程与投标**：技术文件解析 → 选材匹配 → 方案/BOM/报价草稿与二次审核（M2，`docs/engineering.md`）、招标拆解与偏离表/资审提醒（`docs/tender.md`）。
- **产供销主干（M3）**：销售合同/订单/发货/签收（`docs/orders.md`）、约束求解排窑与交期评估（`docs/planning.md`）、实时库存/请购/比价/采购到货（`docs/supply.md`）。
- **质量、财务与设备（M4）**：来料/成品检验、自动 NCR 与 8D 追溯、质保书、计量器具校准与 ISO 审核包（`docs/quality.md`）；应收/应付账龄、批次成本、订单毛利与金蝶/用友凭证草稿（`docs/finance.md`）；设备台账、预防性保养、故障工单与 MTBF/MTTR（`docs/eam.md`）。
- **全职能扩展与 SaaS 复制（M5）**：EHS 安全环保（`docs/ehs.md`）、人力排班与计件工资（`docs/hr.md`）、研发配方与试产（`docs/rnd.md`）、筑炉施工与烘炉验收（`docs/construction.md`）、经营决策驾驶舱/商机漏斗/全厂月报（`docs/market.md`）、统一 Watchdog 巡检与 SaaS 运营总控（`docs/ops.md`）、49 条零成本黄金评测与 Badcase 回归引擎（`docs/eval.md`）。

## 仓库目录

| 路径 | 内容 |
|---|---|
| `fos/` | Python 核心 OS、15 大业务领域模块、FastAPI 接口、统一 CLI 与 16 个 Web 工作台（`/app/`） |
| `crates/` | Rust Workspace：`fos-protocol` 云边数据契约、`fos-edge` 厂端 Modbus 采集与断网缓存守护进程 |
| `packs/refractory/` | 耐火材料行业包：本体扩展、14 个岗位 Skill、49 条黄金评测数据集 |
| `tenants/<id>/` | 租户目录（`tenant.yaml` 与可选 `policy.yaml`，不含密钥，支持 `fos clone-tenant` 一键复制） |
| `migrations/` | Alembic PostgreSQL 迁移脚本 |
| `deploy/` | Docker Compose（`db`/`api`/`ocr`/`litellm`/`channels`/`watchdog`）与阿里云部署脚本 |

## 本地快速试用与开发

```bash
uv venv && uv pip install -e ".[dev]"
export FOS_DATABASE_URL=sqlite+aiosqlite:///var/fos.db

# 首次初始化并启动 Web 工作台（http://127.0.0.1:8000/app/）
.venv/bin/fos quickstart && .venv/bin/fos samples ./samples && .venv/bin/fos serve

# 常用运维与验证命令
.venv/bin/fos eval luneng                                     # 运行 49 条黄金数据集回归评测
.venv/bin/fos security-check luneng                           # 运行租户安全与上线基线自检
.venv/bin/fos watchdog --all                                  # 执行全厂统一 Watchdog 巡检
.venv/bin/fos monthly-report luneng --xlsx monthly.xlsx       # 生成全厂经营月报（Markdown + Excel）
.venv/bin/fos clone-tenant huadong-ref 华东耐材二厂            # 一键零代码复制第二家工厂并验证就绪

# 完整测试与代码检查
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/pytest -q
cargo test --workspace
```

## Docker Compose 生产部署

阿里云 ECS（含 GPU 上部署 Xiaomi-OCR-0）一键步骤见 `docs/deploy-aliyun.md`。手动编排示例：

```bash
cp deploy/.env.example deploy/.env      # 填入数据库密码、百炼/DeepSeek Key 与 IM 凭据（勿提交）
# 默认启动 db + api；按需叠加 --profile gpu / llm / im / ops（Watchdog 守护巡检）
docker compose -f deploy/docker-compose.yml --env-file deploy/.env --profile ops up -d --build
docker compose -f deploy/docker-compose.yml exec api fos quickstart --login jayq --display-name JayQ
```

默认 `FOS_LLM_MODE=mock` 不调用外部模型；生产环境请将 `FOS_LLM_MODE=litellm` 接入百炼 DeepSeek/Qwen，并设置 `FOS_ENV=prod`。
