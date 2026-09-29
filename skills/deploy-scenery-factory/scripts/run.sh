#!/usr/bin/env bash
# 前台启动短视频工厂 FastAPI 服务（Ctrl-C 停止）
# 用法: bash skills/deploy-scenery-factory/scripts/run.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT"
PY="${SVF_VENV:-$ROOT/.venv}/bin/python"
PORT="${SVF_PORT:-8600}"

if [ ! -x "$PY" ]; then
  echo "错误：未找到 $PY，请先运行 scripts/deploy.sh"
  exit 1
fi
if [ ! -f .env ]; then
  echo "错误：缺少 .env，请先运行 scripts/deploy.sh（或 cp .env.example .env）"
  exit 1
fi

echo "启动 Scenery-Factory：http://0.0.0.0:$PORT/  (Ctrl-C 停止)"
exec "$PY" -m apps.api
