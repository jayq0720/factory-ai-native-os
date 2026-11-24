# Prometheus + Grafana 生产监控接入指南

本目录包含与 `GET /v1/ops/metrics` 严格对齐（并由自动化测试交叉校验防漂移）的监控与告警配置：

| 文件 | 用途 |
|---|---|
| `prometheus.yml` | Prometheus 抓取配置（Bearer Token 文件注入，30s 采集间隔） |
| `alerts.yml` | 9 条生产 SRE 告警规则（5xx、慢响应、Token 预算预警/熔断、安全基线、行业包、待审批积压、窑炉告警、网关离线） |
| `grafana-dashboard.json` | 10 面板 Grafana 运维大盘（支持按 `$tenant` 切换工厂视图） |

## 快速接入步骤

1. 为 Prometheus 签发专用只读超管密钥并写入受限文件（权限 `600`，严禁提交仓库）：
   ```bash
   fos issue-key luneng admin --label prometheus-scraper > /etc/prometheus/secrets/fos_ops_token
   chmod 600 /etc/prometheus/secrets/fos_ops_token
   ```
2. 将 `prometheus.yml` 与 `alerts.yml` 挂载至 Prometheus 配置目录并重载；
3. 在 Grafana 中选择 **Dashboards → Import → Upload JSON file** 导入 `grafana-dashboard.json` 并选择对应 Prometheus 数据源即可。
