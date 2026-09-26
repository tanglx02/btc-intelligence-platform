#!/usr/bin/env bash
# ==========================================================
# BTC 全市场智能研究平台 - 一键启动开发环境 (Linux / macOS)
# 用法: ./scripts/dev-start.sh
# ==========================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "==> [1/4] 检查环境变量文件"
if [ ! -f .env ]; then
  echo "    未找到 .env，从 .env.example 复制一份"
  cp .env.example .env
fi

echo "==> [2/4] 启动基础设施 (Postgres + Redis + Adminer)"
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d

echo "==> [3/4] 安装依赖"
( cd backend && uv sync --extra dev )
( cd frontend && pnpm install )

echo "==> [4/4] 启动后端与前端开发服务器"
( cd backend && uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000 ) &
BACKEND_PID=$!
( cd frontend && pnpm dev ) &
FRONTEND_PID=$!

trap 'echo "==> 停止服务"; kill $BACKEND_PID $FRONTEND_PID 2>/dev/null || true' EXIT

echo ""
echo "开发环境已启动:"
echo "  - Backend API : http://localhost:8000  (docs: /docs)"
echo "  - Frontend    : http://localhost:3000"
echo "  - Adminer     : http://localhost:8080"
echo "按 Ctrl+C 停止全部服务"

wait
