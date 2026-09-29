#!/usr/bin/env bash
# 安装依赖并生成 .env（不启动服务）
# 用法: bash skills/deploy-scenery-factory/scripts/deploy.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT"
VENV="${SVF_VENV:-$ROOT/.venv}"
PY="$VENV/bin/python"

[ -f requirements.txt ] || { echo "错误：未在 $ROOT 找到 requirements.txt，请在仓库根运行"; exit 1; }

echo "== 1/3 创建虚拟环境 =="
if [ -d "$VENV" ]; then
  echo "  $VENV 已存在，复用"
else
  python3 -m venv "$VENV"
  echo "  已创建 $VENV"
fi
"$PY" -m pip install --upgrade pip

echo "== 2/3 安装依赖 (.venv/bin/pip install -r requirements.txt) =="
"$PY" -m pip install -r requirements.txt

echo "== 3/3 生成 .env =="
if [ -f .env ]; then
  echo "  .env 已存在，跳过（需重置请先手动删除 .env）"
else
  cp .env.example .env
  echo "  已生成 .env：请编辑填入模型 provider 配置"
  echo "  - 赛事首选 StepFun: TEXT_MODEL_PROVIDER=custom + SVF_CUSTOM_BASE/MODEL/API_KEY"
  echo "  - 离线:            TEXT_MODEL_PROVIDER=ollama"
fi

echo
echo "部署完成。"
echo "  启动: bash skills/deploy-scenery-factory/scripts/run.sh"
echo "  验收: bash skills/deploy-scenery-factory/scripts/verify.sh"
