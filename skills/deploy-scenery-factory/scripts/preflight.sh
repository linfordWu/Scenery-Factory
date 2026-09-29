#!/usr/bin/env bash
# 部署前环境自检：Python / venv / pip / 关键文件 / 端口 / 依赖服务
# 用法: bash skills/deploy-scenery-factory/scripts/preflight.sh
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PORT="${SVF_PORT:-8600}"
COMFY_BASE="${COMFY_BASE:-http://localhost:8188}"
QWEN_URL="${QWEN_IMAGE_URL:-http://localhost:8601}"

FAILED=0
ok()   { printf '  [ OK ] %s\n' "$1"; }
warn() { printf '  [WARN] %s\n' "$1"; }
fail() { printf '  [FAIL] %s\n' "$1"; FAILED=1; }

echo "== Scenery-Factory 部署前自检 =="
echo "仓库根: $ROOT"
echo

echo "-- Python 运行时 --"
if command -v python3 >/dev/null 2>&1; then
  PYV="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  ok "python3 $PYV"
  if python3 -c 'import sys;raise SystemExit(0 if sys.version_info>=(3,10) else 1)'; then
    ok "Python 版本 >= 3.10"
  else
    warn "建议 Python >= 3.10（当前 $PYV）"
  fi
  python3 -m venv --help >/dev/null 2>&1 && ok "venv 可用" || fail "venv 不可用（apt install python3-venv）"
  python3 -m pip --version >/dev/null 2>&1 && ok "pip 可用" || fail "pip 不可用"
else
  fail "未找到 python3"
fi
echo

echo "-- 关键文件 --"
for f in requirements.txt requirements-dev.txt .env.example apps/api/__main__.py; do
  if [ -f "$ROOT/$f" ]; then ok "存在 $f"; else fail "缺失 $f"; fi
done
echo

echo "-- 端口占用（$PORT）--"
if command -v ss >/dev/null 2>&1; then
  if ss -ltn 2>/dev/null | grep -q ":$PORT "; then warn "端口 $PORT 已被占用"; else ok "端口 $PORT 空闲"; fi
elif command -v lsof >/dev/null 2>&1; then
  if lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then warn "端口 $PORT 已被占用"; else ok "端口 $PORT 空闲"; fi
else
  warn "缺少 ss/lsof，跳过端口检测"
fi
echo

echo "-- 依赖服务 --"
if command -v curl >/dev/null 2>&1; then
  if curl -fsS --max-time 3 "$COMFY_BASE/system_stats" >/dev/null 2>&1; then
    ok "ComfyUI 可达 ($COMFY_BASE)"
  else
    warn "ComfyUI 不可达 ($COMFY_BASE)：渲染前需先启动"
  fi
  if curl -fsS --max-time 3 "$QWEN_URL/" >/dev/null 2>&1; then
    ok "Qwen-Image 可达 ($QWEN_URL)"
  else
    warn "Qwen-Image 不可达 ($QWEN_URL)：仅「定妆 / 场景 / 首帧」需要"
  fi
else
  warn "缺少 curl，跳过依赖服务探测"
fi
echo

if [ "$FAILED" -eq 0 ]; then
  echo "自检通过：可继续部署。"
else
  echo "存在 [FAIL] 项：请先修复后再部署。"
fi
exit "$FAILED"
