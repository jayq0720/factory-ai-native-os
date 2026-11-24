# Factory AI Native OS

让没有任何信息化系统的工厂，跳过"先上 MES/ERP"，直接由常驻在线的工厂 Agent 承担录入、追溯、监控、分析、审批与提醒。当前内置三大行业包：**耐火材料（`packs/refractory`，首个试点六安绿能）**、**工业陶瓷（`packs/ceramics`）** 与 **精密铸造（`packs/foundry`）**，支持零代码多租户与跨行业扩展。

- 架构与 31 周路线图：`docs/development-plan.md`（**M1–M5 全职能里程碑 + 生产级 SRE/灾备/治理体系已完成**）
- 工程规范与代码地图：`CLAUDE.md`
- 演示页面：`demo/index.html`（模拟数据演示）

## 核心能力全景（M1–M5 全职能闭环 + 生产 SRE 体系）

- **底层 OS 与安全治理**：三层企业本体（核心/行业包/租户）、不可变事件账本（`Ledger.append()` + 证据链）、RBAC 权限与审批红线引擎、三端 IM 适配器（钉钉 Stream / 飞书 / 企业微信）、Fast Path + Router/Worker 分层调度、按租户 LLM Token 预算硬熔断、滑动窗口 API 限流与账号/密钥在线治理。
- **生产与现场**：拍照录单与批次双向追溯（M1，`docs/production-entry.md`）、Rust 厂端网关 `fos-edge`（含 `doctor [--json]` 上线前自检与 `status --json` 探针）+ 窑炉曲线监控与能耗分析（`docs/fos-edge.md`、`docs/kiln-monitoring.md`）。
- **工程与投标**：技术文件解析 → 选材匹配 → 方案/BOM/报价草稿与二次审核（M2，`docs/engineering.md`）、招标拆解与偏离表/资审提醒（`docs/tender.md`）。
- **产供销主干（M3）**：销售合同/订单/发货/签收（`docs/orders.md`）、约束求解排窑与交期评估（`docs/planning.md`）、实时库存/请购/比价/采购到货（`docs/supply.md`）。
- **质量、财务与设备（M4）**：来料/成品检验、自动 NCR 与 8D 追溯、质保书、计量器具校准与 ISO 审核包（`docs/quality.md`）；应收/应付账龄、批次成本、订单毛利与金蝶/用友凭证草稿（`docs/finance.md`）；设备台账、预防性保养、故障工单与 MTBF/MTTR（`docs/eam.md`）。
- **全职能扩展、SRE 与灾备（M5+）**：EHS 安全环保（`docs/ehs.md`）、人力排班与计件工资（`docs/hr.md`）、研发配方与试产（`docs/rnd.md`）、筑炉施工与烘炉验收（`docs/construction.md`）、经营决策驾驶舱/商机漏斗/定时月报（`docs/market.md`）、统一 Watchdog 巡检 + Prometheus/Grafana 监控告警包 + 不可变账本实时一致性审计/防篡改灾备恢复 + 脱敏诊断包 + 云边一键 `doctor` 诊断（`docs/ops.md`）、三行业包 75 条零成本黄金评测与 Badcase 回归引擎（`docs/eval.md`）。

## 仓库目录

| 路径 | 内容 |
|---|---|
| `fos/` | Python 核心 OS、15 大业务领域模块、FastAPI 接口、统一 CLI 与 16 个 Web 工作台（`/app/`） |
| `crates/` | Rust Workspace：`fos-protocol` 云边数据契约、`fos-edge` 厂端 Modbus 采集与断网缓存守护进程 |
| `packs/refractory/` | 耐火材料行业包：本体扩展、14 个岗位 Skill、52 条黄金评测数据集 |
| `packs/ceramics/` | 工业陶瓷行业包：本体/EHS/EAM/市场扩展、陶瓷工艺与安环 Skill、11 条黄金评测数据集 |
| `packs/foundry/` | 精密铸造行业包：熔炼炉次/模具/金相本体扩展、铸造工艺与安环 Skill、12 条黄金评测数据集 |
| `tenants/<id>/` | 租户目录（`tenant.yaml` 与可选 `policy.yaml`，不含密钥，支持 `fos clone-tenant` 一键复制） |
| `migrations/` | Alembic PostgreSQL 迁移脚本 |
| `deploy/` | Docker Compose（`db`/`api`/`ocr`/`litellm`/`channels`/`watchdog`/`backup`）、Kubernetes Kustomize（`deploy/k8s/`）、Prometheus/Grafana 监控包（`deploy/monitoring/`）与阿里云部署脚本 |

## 本地快速试用与开发

```bash
uv venv && uv pip install -e ".[dev]"
export FOS_DATABASE_URL=sqlite+aiosqlite:///var/fos.db

# 首次初始化并启动 Web 工作台（http://127.0.0.1:8000/app/）
.venv/bin/fos quickstart && .venv/bin/fos samples ./samples && .venv/bin/fos serve

# 常用运维、灾备与门禁命令
.venv/bin/fos doctor --json                                   # 云端控制面八维一键自检
.venv/bin/fos validate-pack                                   # 校验全部行业包本体/Skill/黄金评测集
.venv/bin/fos eval --all                                      # 运行全租户黄金数据集回归评测
.venv/bin/fos security-check --all --json                     # 运行全租户安全与上线基线自检
.venv/bin/fos verify-ledger --all --json                      # 实时只读审计全租户账本完整性
.venv/bin/fos watchdog --all                                  # 执行全厂统一 Watchdog 巡检
.venv/bin/fos monthly-report luneng --xlsx monthly.xlsx       # 生成全厂经营月报（Markdown + Excel）
.venv/bin/fos backup-ledger --all --out-dir ./backups         # 全租户防篡改灾备（落盘自检 + 保留清理）
.venv/bin/fos support-bundle luneng ./support-bundle.zip      # 导出脱敏 SRE 运维诊断包（零明文密钥）
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
# 默认启动 db + api；按需叠加 --profile gpu / llm / im / ops（Watchdog + 每日自动灾备守护）
docker compose -f deploy/docker-compose.yml --env-file deploy/.env --profile ops up -d --build
docker compose -f deploy/docker-compose.yml exec api fos quickstart --login jayq --display-name JayQ
docker compose -f deploy/docker-compose.yml exec api fos doctor
```

默认 `FOS_LLM_MODE=mock` 不调用外部模型；生产环境请将 `FOS_LLM_MODE=litellm` 接入百炼 DeepSeek/Qwen，并设置 `FOS_ENV=prod`。
