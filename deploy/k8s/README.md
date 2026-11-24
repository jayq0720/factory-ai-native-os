# Kubernetes 生产部署指南（ACK / k3s / 原生 K8s）

本目录提供零外部依赖、符合非 root 最小权限与双探针基线的 Kustomize 部署清单。

## 1. 创建机密凭据（禁止提交仓库，生产推荐 ExternalSecrets / KMS）

```bash
kubectl create namespace fos --dry-run=client -o yaml | kubectl apply -f -
kubectl -n fos create secret generic fos-secrets \
  --from-literal=database-url='postgresql+asyncpg://fos:<密码>@<RDS内网地址>:5432/fos' \
  --from-literal=litellm-api-key='<LiteLLM-Master-Key>' \
  --from-literal=ocr-api-key='<百炼-API-Key>' \
  --from-literal=edge-command-key='<fos edge-keygen 生成的私钥>'
```

## 2. 一键应用清单与验证

```bash
kubectl apply -k deploy/k8s
kubectl -n fos rollout status deployment/fos-api
kubectl -n fos exec deploy/fos-api -- fos doctor
```

> 生产环境请将 `deployment-api.yaml` 中的 `blobs` 与 `cronjob-ops.yaml` 中的 `backups` 卷替换为持久化 PVC / NAS / OSS 存储。
