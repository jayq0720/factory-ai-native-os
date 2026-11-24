# SaaS 生产运维、安全治理、灾备与多行业复制手册

本文档汇总 Factory AI Native OS 的生产级运维、SRE 可观测性、不可变账本灾备恢复、成本熔断及多工厂/多行业复制体系。Web 运营总控台位于 `/app/ops.html`。

## 1. 全厂统一 Watchdog 巡检（`fos/ops/watchdog.py`）

- 单次幂等触发当前租户所有已启用模块的到期提醒（计量校准、财务逾期应收/应付、设备保养、EHS 隐患超期、HR 特种作业证照临期），并自动清理超过 `expires_at` 的过期审批单。
- **手动/API 触发**：`POST /v1/ops/watchdog/run` 或 `fos watchdog <tenant>`。
- **常驻守护循环**：`fos watchdog --all --interval 1800`，对应 Docker Compose `--profile ops` 中的 `watchdog` 服务。

## 2. SRE 可观测性与合规审计（`fos/ops/metrics.py`）

- **健康探针与一键自检**：`GET /healthz`（存活）与 `GET /readyz`（数据库连通性及模式状态），均豁免 API 限流；全平台控制面自检使用 `fos doctor [--json] [--skip-db]` 或 `GET /v1/ops/doctor`（需 `*.*:*` 权限，一次校验运行环境、原件目录、行业包、租户配置、数据库与租户 Token 预算水位）。
- **响应追踪头**：所有 HTTP 响应自动附带 `X-Request-Id` 与 `X-Response-Time-Ms`。
- **零依赖 Prometheus 指标（`GET /v1/ops/metrics`）**：需 `*.*:*` 平台超级管理员权限，导出：
  - HTTP 指标：`fos_http_requests_total`、`fos_http_request_duration_seconds_{sum,count}`；
  - 租户指标：`fos_tenants_total`、`fos_events_total`、`fos_open_todos`、`fos_pending_approvals`、`fos_llm_tokens_7d`、`fos_security_baseline_ok`；
  - 成本与行业包指标：`fos_budget_daily_ratio`、`fos_budget_monthly_ratio`、`fos_budget_allowed`、`fos_industry_packs_total`、`fos_industry_pack_ok`；
  - 开箱即用抓取与告警规则包：`deploy/monitoring/prometheus.yml` 与 `deploy/monitoring/alerts.yml`（覆盖 5xx 错误率、慢响应、Token 预算预警/硬熔断、安全基线、行业包健康度与待审批积压）。
- **不可变账本审计流水**：`GET /v1/ops/audit`（按 `event_prefix` / `object_type` / `actor_id` / `data_mode` 倒序分页），以及 `GET /v1/ops/audit.xlsx`（防公式注入合规审计 Excel 导出）。

## 3. 成本熔断与访问防护（`fos/governance/`）

- **LLM Token 预算租约（`fos/governance/budget.py`）**：
  - 在 `tenants/<id>/tenant.yaml` 的 `budget` 段配置 `daily_tokens`、`monthly_tokens` 与 `warn_ratio`（0 表示不限）；
  - `AgentRuntime` 调用前硬校验，超额时 0 次调用 LLM 并引导使用零成本 Fast Path 指令；实时查询：`GET /v1/ops/budget`。
- **滑动窗口 API 限流（`fos/governance/ratelimit.py`）**：
  - 通过环境变量 `FOS_RATE_LIMIT_PER_MIN` 配置（默认 `600`，`0` 关闭），按 API Key ID 或来源 IP 分桶，超限返回 `429` + `Retry-After`。
- **账号、角色、API Key 与 IM 绑定在线治理**：
  - 账号与角色：`GET/POST/PATCH /v1/ops/users`（离职停用瞬间封禁全部 Key，内置管理员防自锁保护）；
  - 密钥生命周期：`GET/POST /v1/ops/keys`、`POST /v1/ops/keys/{key_id}/revoke`（脱敏列举 + 跨租户隔离）；
  - IM 绑定覆盖率：`GET /v1/ops/im-bindings`。

## 4. 不可变账本防篡改灾备与恢复（`fos/ops/backup.py`）

完整闭环：`backup-ledger` → `verify-backup` → `restore-ledger`：

```bash
# 1. 导出单租户真实账本防篡改 JSONL（含 _meta 头与 SHA-256 校验尾）
fos backup-ledger luneng ./backups/luneng_2026-11.jsonl
# 或通过 API：GET /v1/ops/backup.jsonl

# 2. 离线校验完整性（任意单字符篡改、删行或租户串扰均返回非零退出码）
fos verify-backup ./backups/luneng_2026-11.jsonl

# 3. 向全新空租户原子恢复并自动重建全量投影与 TenantCounter 序号（严守账本只追加红线）
fos restore-ledger luneng ./backups/luneng_2026-11.jsonl

# 4. 全租户批量自检灾备与保留策略清理（--profile ops 中 backup 服务默认每 24h 自动执行）
fos backup-ledger --all --out-dir ./backups --keep 30
```

## 5. 多工厂复制、多行业包与厂端网关自检

- **一键复制同行业新工厂**：`fos clone-tenant <new_id> <工厂全称> [--pack refractory|ceramics] [--im dingtalk,feishu]`，自动落盘配置并执行 `verify_tenant_replication()` 校验。
- **行业包独立校验与目录**：`fos validate-pack [pack]`（已接入 CI 硬门禁）与 `GET /v1/ops/packs`，当前内置 `refractory`（耐火材料）与 `ceramics`（工业陶瓷）双行业包。
- **黄金评测与安全基线门禁**：`fos eval <tenant>`、`fos security-check <tenant>`。
- **厂端 Rust 网关 `fos-edge`**：`fos-edge doctor <edge.toml> [--json]`（上线前一键自检与机器可读 JSON）与 `fos-edge status <edge.toml> --json`（监控探针 JSON 输出）。
