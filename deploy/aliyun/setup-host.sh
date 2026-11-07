#!/usr/bin/env bash
# 阿里云 ECS 主机初始化（Ubuntu 22.04 / 24.04，root 执行）：装 Docker + Compose 插件；
# 有 NVIDIA GPU 时再装 NVIDIA Container Toolkit，让容器能用 GPU。可重复执行。
#
#   sudo bash deploy/aliyun/setup-host.sh
#   可选：DOCKER_MIRROR=https://xxxx.mirror.aliyuncs.com   （容器镜像服务 ACR → 镜像工具 → 镜像加速器 里的专属地址）
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "请用 root 或 sudo 执行"; exit 1; }
. /etc/os-release
[ "${ID}" = ubuntu ] || echo "提示：脚本按 Ubuntu 编写，${ID} 上可能需要手动调整"

echo "== 1/3 安装 Docker（阿里云镜像源）"
if ! command -v docker >/dev/null; then
  apt-get update -y
  apt-get install -y ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://mirrors.aliyun.com/docker-ce/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://mirrors.aliyun.com/docker-ce/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker

if [ -n "${DOCKER_MIRROR:-}" ]; then
  echo "== 配置镜像加速器 ${DOCKER_MIRROR}"
  mkdir -p /etc/docker
  python3 - "$DOCKER_MIRROR" <<'PY'
import json, sys, pathlib
p = pathlib.Path("/etc/docker/daemon.json")
cfg = json.loads(p.read_text()) if p.exists() and p.read_text().strip() else {}
mirrors = cfg.setdefault("registry-mirrors", [])
if sys.argv[1] not in mirrors:
    mirrors.append(sys.argv[1])
p.write_text(json.dumps(cfg, indent=2))
PY
  systemctl restart docker
fi

echo "== 2/3 GPU"
if command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
  if ! command -v nvidia-ctk >/dev/null; then
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
      | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
      > /etc/apt/sources.list.d/nvidia-container-toolkit.list
    apt-get update -y
    apt-get install -y nvidia-container-toolkit
  fi
  nvidia-ctk runtime configure --runtime=docker
  systemctl restart docker
  echo "GPU 已就绪"
else
  echo "没有检测到 NVIDIA 驱动：只能用 mock 或 qwen_vl（百炼）模式。"
  echo "GPU 实例请在创建时勾选「安装 GPU 驱动」，或按阿里云文档手动安装驱动后重跑本脚本。"
fi

echo "== 3/3 数据目录"
mkdir -p /data/models
docker --version && docker compose version
echo "完成。下一步：bash deploy/aliyun/download-model.sh（GPU 模式）或直接 bash deploy/aliyun/deploy.sh"
