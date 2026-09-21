#!/usr/bin/env bash
# JEV 量化交易系统一键启动脚本
#
#   ./start.sh          构建前端并由 FastAPI 单端口托管  ->  http://localhost:8000
#   ./start.sh dev      后端 :8000 + Vite 热更新前端 :5173
#   ./start.sh backend  只跑后端（前端可用 npm run dev 另开）
#   ./start.sh test     跑后端测试
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
VENV="$ROOT/.venv"
PORT="${PORT:-8000}"
MODE="${1:-all}"

log() { printf '\033[1;36m[jev]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[jev]\033[0m %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null || die "未找到 python3（需要 3.11+）"

# ---- 加载 .env（若存在）----
if [ -f "$ROOT/.env" ]; then
  log "加载 $ROOT/.env"
  set -a; . "$ROOT/.env"; set +a
fi

# ---- Python 环境 ----
if [ ! -d "$VENV" ]; then
  log "创建虚拟环境 $VENV"
  python3 -m venv "$VENV"
fi
PY="$VENV/bin/python"
log "安装后端依赖…"
"$PY" -m pip install -q --upgrade pip >/dev/null
"$PY" -m pip install -q -r "$BACKEND/requirements.txt"

if [ "${TYPESAFE_API_KEY:-}" = "" ]; then
  log "未配置 TYPESAFE_API_KEY：Jev 决策将自动降级为本地 Mock 动量模型（功能完整可演示）"
else
  log "已配置 TYPESAFE_API_KEY：使用线上 Jev 决策大模型"
fi

if [ "$MODE" = "test" ]; then
  "$PY" -m pip install -q -r "$BACKEND/requirements-dev.txt"
  cd "$BACKEND" && exec "$PY" -m pytest -q
fi

PIDS=()
cleanup() {
  log "正在停止…"
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ---- 前端 ----
if [ "$MODE" = "all" ] || [ "$MODE" = "dev" ]; then
  command -v npm >/dev/null || die "未找到 npm（需要 Node 18+），或改用 ./start.sh backend"
  if [ ! -d "$FRONTEND/node_modules" ]; then
    log "安装前端依赖（首次约 1 分钟）…"
    (cd "$FRONTEND" && npm install --no-audit --no-fund)
  fi
fi

case "$MODE" in
  all)
    log "构建前端…"
    (cd "$FRONTEND" && npm run build)
    log "启动后端（同时托管前端）: http://localhost:$PORT"
    cd "$BACKEND" && exec "$VENV/bin/uvicorn" app.server:app --host 0.0.0.0 --port "$PORT"
    ;;
  backend)
    log "启动后端: http://localhost:$PORT  （API 文档 /docs）"
    cd "$BACKEND" && exec "$VENV/bin/uvicorn" app.server:app --host 0.0.0.0 --port "$PORT" --reload
    ;;
  dev)
    log "启动后端 :$PORT"
    (cd "$BACKEND" && "$VENV/bin/uvicorn" app.server:app --host 0.0.0.0 --port "$PORT" --reload) &
    PIDS+=($!)
    log "启动前端开发服务器 :5173（已配置 /api 代理到后端）"
    (cd "$FRONTEND" && npm run dev) &
    PIDS+=($!)
    log "就绪 ->  前端 http://localhost:5173   后端 http://localhost:$PORT/docs"
    wait -n
    ;;
  *)
    die "未知参数：$MODE（可选 all / dev / backend / test）"
    ;;
esac
