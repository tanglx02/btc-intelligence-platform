#!/usr/bin/env bash
# ==========================================================
# BTC 全市场智能研究平台 - 一键启动开发环境 (Linux / macOS)
# 用法: ./scripts/dev-start.sh
# 流程: .env 检查 -> 基础设施 -> 依赖安装 -> 迁移 -> 前后端热重载
# ==========================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail() { echo -e "${RED}[ERROR]${NC} $1" >&2; exit 1; }

# ---------- [0/5] 依赖检查 ----------
echo "==> [0/5] 检查依赖"
command -v docker >/dev/null 2>&1 || fail "未安装 docker（https://docs.docker.com/engine/install/）"
docker info >/dev/null 2>&1 || fail "Docker 守护进程未运行，请先启动 Docker"
command -v uv >/dev/null 2>&1 || fail "未安装 uv（https://docs.astral.sh/uv/）"
command -v pnpm >/dev/null 2>&1 || warn "未安装 pnpm，将尝试使用 corepack 启用: corepack enable pnpm"
if ! command -v pnpm >/dev/null 2>&1; then
    corepack enable pnpm || fail "pnpm 启用失败，请手动安装: npm install -g pnpm"
fi

echo "==> [1/5] 检查环境变量文件"
if [ ! -f .env ]; then
    echo "    未找到 .env，从 .env.example 复制一份"
    cp .env.example .env
fi

echo "==> [2/5] 启动基础设施 (Postgres + Redis + Adminer)"
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d

# 等待 postgres healthy（最多 120s）
ELAPSED=0
until [ "$(docker inspect -f '{{.State.Health.Status}}' btc_postgres 2>/dev/null || echo starting)" = "healthy" ]; do
    ELAPSED=$((ELAPSED + 2))
    if [ "$ELAPSED" -ge 120 ]; then
        fail "等待 PostgreSQL 就绪超时（120s），请查看日志: docker compose logs postgres"
    fi
    sleep 2
done
info "PostgreSQL 已就绪"

echo "==> [3/5] 安装依赖"
( cd backend && uv sync --extra dev )
( cd frontend && pnpm install )

echo "==> [4/5] 执行数据库迁移（幂等，可重复执行）"
( cd backend && uv run alembic upgrade head )
info "如需种子数据（providers/indicators/events）: cd backend && uv run python scripts/seed_data.py"

echo "==> [5/5] 启动后端与前端开发服务器"
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
