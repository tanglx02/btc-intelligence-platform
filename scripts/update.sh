#!/usr/bin/env bash
# =============================================================
# update.sh - BTC 平台升级更新脚本
# 流程: git pull -> rebuild -> alembic upgrade head -> 重启应用服务
# 数据卷（postgres_data / redis_data）全程保留
# 用法: bash scripts/update.sh
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail() { echo -e "${RED}[ERROR]${NC} $1" >&2; exit 1; }

compose() { docker compose -f docker-compose.prod.yml "$@"; }

wait_healthy() {
    local container="$1" timeout="${2:-180}" elapsed=0
    until [ "$(docker inspect -f '{{.State.Health.Status}}' "$container" 2>/dev/null || echo starting)" = "healthy" ]; do
        elapsed=$((elapsed + 2))
        if [ "$elapsed" -ge "$timeout" ]; then
            fail "等待 $container 就绪超时（${timeout}s）"
        fi
        sleep 2
    done
}

wait_backend_ready() {
    local elapsed=0
    until compose exec -T backend python -c "import app" >/dev/null 2>&1; do
        elapsed=$((elapsed + 2))
        if [ "$elapsed" -ge 90 ]; then
            fail "backend 容器 90s 内未就绪，请查看日志: bash scripts/manage.sh"
        fi
        sleep 2
    done
}

echo ""
info "===== BTC 平台升级开始 ====="

# ---------- 1. 升级前备份 ----------
info "[1/6] 升级前备份数据库..."
if [ -f scripts/backup.sh ]; then
    bash scripts/backup.sh || warn "升级前备份失败（继续升级，风险自负）"
fi

# ---------- 2. 拉取新代码 ----------
info "[2/6] 拉取最新代码..."
if [ -d .git ]; then
    git pull --ff-only || fail "git pull 失败（存在无法快进的本地改动），请手动处理后重试"
    git log --oneline -1
else
    warn "非 git 仓库，跳过代码拉取（直接使用当前目录构建）"
fi

# ---------- 3. 重新构建镜像 ----------
info "[3/6] 重新构建镜像..."
compose build

# ---------- 4. 数据库迁移 ----------
info "[4/6] 执行数据库迁移（Alembic，保留现有数据）..."
compose up -d postgres
wait_healthy btc_postgres 180
compose run --rm --no-deps backend alembic upgrade head

# ---------- 5. 重启应用服务 ----------
info "[5/6] 重启 backend / scheduler / frontend..."
compose up -d --force-recreate backend scheduler frontend
wait_backend_ready

# ---------- 6. 验证 ----------
info "[6/6] 验证升级结果..."
sleep 5
if compose exec -T backend curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
    info "后端健康检查通过"
else
    fail "升级后 backend 健康检查失败！请立即检查日志并考虑回滚（git checkout <上一 commit> 后重跑本脚本）"
fi
if curl -fsS http://localhost/health >/dev/null 2>&1; then
    info "Nginx 入口健康检查通过"
else
    warn "Nginx 入口暂未通过（可能仍在启动），稍后可用 curl http://localhost/health 复查"
fi
compose ps

echo ""
info "===== 升级完成（数据卷未受影响） ====="
