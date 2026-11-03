#!/usr/bin/env bash
# 一键部署 / 升级。在仓库根目录执行，可重复执行（升级时 git pull 后再跑一次）。
#
#   bash deploy/aliyun/deploy.sh gpu       本机 GPU 跑 Xiaomi-OCR-0（先执行 download-model.sh）
#   bash deploy/aliyun/deploy.sh bailian   用百炼 Qwen-VL-OCR，需要在 deploy/.env 填 FOS_OCR_API_KEY
#   bash deploy/aliyun/deploy.sh remote    OCR 在另一台 GPU 机上，需要在 deploy/.env 填 FOS_OCR_BASE_URL
#   bash deploy/aliyun/deploy.sh mock      不接 OCR（Word / Excel / 文字版 PDF 仍是真实结果）
set -euo pipefail
MODE="${1:-}"
case "$MODE" in gpu|bailian|remote|mock) ;; *) sed -n 2,8p "$0"; exit 1 ;; esac

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
ENV=deploy/.env
DC=(docker compose -f deploy/docker-compose.yml --env-file "$ENV")

# --- 生成 / 更新 deploy/.env：首次生成随机密码，之后只改 OCR 模式，不覆盖已填的值 ---
if [ ! -f "$ENV" ]; then
  cp deploy/.env.example "$ENV"
  sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 16)/" "$ENV"
  sed -i "s/^LITELLM_MASTER_KEY=.*/LITELLM_MASTER_KEY=sk-$(openssl rand -hex 16)/" "$ENV"
  chmod 600 "$ENV"
  echo "已生成 $ENV（随机数据库密码），请妥善保管，不要提交到仓库"
fi
setenv() { if grep -q "^$1=" "$ENV"; then sed -i "s#^$1=.*#$1=$2#" "$ENV"; else echo "$1=$2" >> "$ENV"; fi; }
getenv() { grep "^$1=" "$ENV" | tail -1 | cut -d= -f2-; }

PROFILES=()
case "$MODE" in
  gpu)
    DIR="$(getenv OCR_MODEL_DIR)"; DIR="${DIR:-/data/models/Xiaomi-OCR-0}"
    [ -f "$DIR/config.json" ] || { echo "找不到模型权重 $DIR，先执行：bash deploy/aliyun/download-model.sh"; exit 1; }
    docker info 2>/dev/null | grep -qi nvidia || { echo "Docker 里没有 nvidia 运行时，先执行：sudo bash deploy/aliyun/setup-host.sh"; exit 1; }
    setenv FOS_OCR_MODE xiaomi; setenv FOS_OCR_BASE_URL http://ocr:8001/v1; setenv FOS_OCR_MODEL xiaomi-ocr-0
    PROFILES=(--profile gpu) ;;
  bailian)
    [ -n "$(getenv FOS_OCR_API_KEY)" ] || { echo "请先在 $ENV 填 FOS_OCR_API_KEY=<百炼 API Key>"; exit 1; }
    setenv FOS_OCR_MODE qwen_vl; setenv FOS_OCR_BASE_URL ""; setenv FOS_OCR_MODEL "" ;;
  remote)
    [ -n "$(getenv FOS_OCR_BASE_URL)" ] || { echo "请先在 $ENV 填 FOS_OCR_BASE_URL=http://<GPU 机内网 IP>:8001/v1"; exit 1; }
    setenv FOS_OCR_MODE xiaomi; [ -n "$(getenv FOS_OCR_MODEL)" ] || setenv FOS_OCR_MODEL xiaomi-ocr-0 ;;
  mock)
    setenv FOS_OCR_MODE mock ;;
esac
[ "$(getenv FOS_LLM_MODE)" = litellm ] && PROFILES+=(--profile llm)

echo "== 构建并启动（${MODE}）"
"${DC[@]}" "${PROFILES[@]}" up -d --build

echo "== 等待接口就绪"
for _ in $(seq 1 60); do
  [ "$("${DC[@]}" ps api --format '{{.Health}}' 2>/dev/null)" = healthy ] && break
  sleep 5
done
"${DC[@]}" ps

if [ "$MODE" = gpu ]; then
  echo "== 等待 Xiaomi-OCR-0 加载（首次约 1–5 分钟）"
  for _ in $(seq 1 90); do
    [ "$("${DC[@]}" ps ocr --format '{{.Health}}' 2>/dev/null)" = healthy ] && break
    sleep 5
  done
fi

echo "== OCR 自检"
"${DC[@]}" exec -T api fos ocr-check || echo "OCR 自检失败：查看 ${DC[*]} logs ocr / api"

echo "== 管理员账号"
"${DC[@]}" exec -T api fos quickstart --login "${ADMIN_LOGIN:-admin}" --display-name "${ADMIN_NAME:-管理员}"

PORT="$(getenv FOS_HTTP_PORT)"; PORT="${PORT:-8000}"
# 阿里云实例元数据里取公网 IP（EIP 或固定公网 IP）
IP=""
for k in eipv4 public-ipv4; do
  v="$(curl -s -m 3 "http://100.100.100.200/latest/meta-data/$k" 2>/dev/null || true)"
  if [[ "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then IP="$v"; break; fi
done
echo
echo "部署完成：http://${IP:-<服务器公网 IP>}:${PORT}/app/"
echo "记得在 ECS 安全组只对办公网 IP 放行 ${PORT} 端口；不要放行 5432 / 8001。"
