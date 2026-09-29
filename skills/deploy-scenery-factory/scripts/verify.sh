#!/usr/bin/env bash
# 验收：服务健康检查（GET /projects），可选附带 pytest
# 用法: bash skills/deploy-scenery-factory/scripts/verify.sh
#       RUN_TESTS=1 bash skills/deploy-scenery-factory/scripts/verify.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT"
PORT="${SVF_PORT:-8600}"
BASE="http://localhost:$PORT"
PY="${SVF_VENV:-$ROOT/.venv}/bin/python"

echo "== 健康检查 $BASE/projects =="
if command -v curl >/dev/null 2>&1 && curl -fsS --max-time 5 "$BASE/projects" >/dev/null 2>&1; then
  echo "  [ OK ] 服务在线，Web 工作台: $BASE/"
else
  echo "  [FAIL] 无法访问 $BASE/projects（服务未启动或端口不对）"
fi

echo "== 验收接口自检 =="
if [ -x "$PY" ]; then
  "$PY" - <<'PYCODE' || true
import importlib, sys
for m in ("fastapi", "uvicorn", "pydantic"):
    try:
        importlib.import_module(m)
        print(f"  [ OK ] 依赖 {m}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] 依赖 {m}: {exc}")
PYCODE
else
  echo "  [WARN] 未找到虚拟环境 $PY，跳过依赖自检"
fi

if [ "${RUN_TESTS:-0}" = "1" ]; then
  echo "== 单元测试 (pytest) =="
  if [ -x "$PY" ]; then
    "$PY" -m pip install -q -r requirements-dev.txt
    "$PY" -m pytest tests/ -q
  else
    echo "  [WARN] 未找到虚拟环境，跳过 pytest"
  fi
fi

echo
echo "提示：RUN_TESTS=1 bash skills/deploy-scenery-factory/scripts/verify.sh 可一并跑 pytest"
