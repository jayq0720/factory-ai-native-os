#!/usr/bin/env bash
# 从 ModelScope（魔搭，国内直连）下载 Xiaomi-OCR-0 权重到本地目录，约 2 GB。可重复执行（断点续传）。
#
#   bash deploy/aliyun/download-model.sh                     # 默认 /data/models/Xiaomi-OCR-0
#   OCR_MODEL_DIR=/mnt/models/Xiaomi-OCR-0 bash deploy/aliyun/download-model.sh
set -euo pipefail
DIR="${OCR_MODEL_DIR:-/data/models/Xiaomi-OCR-0}"
REPO="${OCR_MODEL_REPO:-SeerRay-Lab/Xiaomi-OCR-0}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"

mkdir -p "$DIR"
VENV=/opt/fos-modelscope
if [ ! -x "$VENV/bin/modelscope" ]; then
  apt-get install -y python3-venv >/dev/null 2>&1 || true
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q -i "$PIP_INDEX_URL" modelscope
fi
echo "下载 $REPO → $DIR"
"$VENV/bin/modelscope" download --model "$REPO" --local_dir "$DIR"

test -f "$DIR/config.json" || { echo "下载不完整：没有 config.json"; exit 1; }
grep -o '"architectures"[^]]*]' "$DIR/config.json" || true
du -sh "$DIR"
echo "完成。deploy/.env 里 OCR_MODEL_DIR=$DIR，然后 bash deploy/aliyun/deploy.sh gpu"
