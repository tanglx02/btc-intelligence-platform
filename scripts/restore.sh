#!/usr/bin/env bash
# =============================================================
# restore.sh - BTC 平台一键恢复脚本
# 流程: 列出备份 -> 选择 -> 确认 -> 停应用 -> pg_restore -> 重启 -> 验证
# 用法: bash scripts/restore.sh [备份文件路径]
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

[ -f .env ] || fail "未找到 .env，请先运行 install.sh"
env_value() {
    grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- | sed 's/^["'\'']//; s/["'\'']$//'
}
PG_USER="$(env_value POSTGRES_USER)"
PG_DB="$(env_value POSTGRES_DB)"
PG_USER="${PG_USER:-btc_admin}"
PG_DB="${PG_DB:-btc_platform}"

BACKUP_DIR="$ROOT_DIR/backups"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   BTC 平台 - 一键恢复工具                ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ---------- 1. 确定备份文件 ----------
if [ $# -ge 1 ]; then
    TARGET_FILE="$1"
    [ -f "$TARGET_FILE" ] || fail "备份文件不存在: $TARGET_FILE"
else
    shopt -s nullglob
    FILES=("$BACKUP_DIR"/btc_platform_*.dump)
    shopt -u nullglob
    [ ${#FILES[@]} -gt 0 ] || fail "未找到任何备份（$BACKUP_DIR/btc_platform_*.dump）。请先执行 bash scripts/backup.sh"

    echo "可用备份列表（按时间倒序）:"
    echo "──────────────────────────────────────────"
    i=1
    for f in $(ls -t "$BACKUP_DIR"/btc_platform_*.dump); do
        size_mb=$(( $(stat -c%s "$f" 2>/dev/null || stat -f%z "$f") / 1024 / 1024 ))
        echo "  [$i] $(basename "$f")  (${size_mb}MB)"
        FILES[$i]="$f"
        i=$((i+1))
    done
    echo "──────────────────────────────────────────"
    read -r -p "选择要恢复的备份编号 [1]: " choice
    choice="${choice:-1}"
    TARGET_FILE="${FILES[$choice]:-}"
    [ -n "$TARGET_FILE" ] && [ -f "$TARGET_FILE" ] || fail "无效选择: $choice"
fi

info "将恢复备份: $TARGET_FILE"

# ---------- 2. 确认 ----------
echo ""
echo -e "${RED}┌────────────────────────────────────────────────────────┐${NC}"
echo -e "${RED}│  警告：恢复操作将用备份覆盖当前数据库的全部数据！      │${NC}"
echo -e "${RED}│  备份之后产生的行情数据、计划变更等都将丢失。          │${NC}"
echo -e "${RED}└────────────────────────────────────────────────────────┘${NC}"
echo ""
read -r -p "输入 RESTORE 确认执行: " confirm
if [ "${confirm:-}" != "RESTORE" ]; then
    info "已取消恢复"
    exit 0
fi

# ---------- 3. 停止应用服务（保留 postgres） ----------
info "[1/4] 停止 backend / scheduler（postgres 保持运行）..."
compose stop backend scheduler || true

# ---------- 4. 恢复数据库 ----------
info "[2/4] 恢复数据库 ${PG_DB}（pg_restore --clean，覆盖现有对象）..."
set +e
compose exec -T postgres pg_restore \
    -U "$PG_USER" -d "$PG_DB" \
    --clean --if-exists --no-owner --no-privileges \
    < "$TARGET_FILE"
RESTORE_RC=$?
set -e
if [ "$RESTORE_RC" -ne 0 ]; then
    warn "pg_restore 返回码 $RESTORE_RC（扩展/权限类警告通常无害，以验证步骤结果为准）"
fi

# ---------- 5. 重启应用服务 ----------
info "[3/4] 重启应用服务..."
compose up -d backend scheduler frontend

# ---------- 6. 验证 ----------
info "[4/4] 恢复后验证..."
sleep 8

TABLE_COUNT=$(compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -t -A -c \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';" || echo "?")
info "public schema 表数量: ${TABLE_COUNT}"

if compose exec -T backend curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
    info "✓ 后端健康检查通过"
else
    warn "✗ 后端暂未就绪（可能仍在启动），稍后执行: curl http://localhost/health"
fi

if compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -t -A -c \
    "SELECT count(*) FROM timescaledb_information.hypertables;" >/dev/null 2>&1; then
    HT_COUNT=$(compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -t -A -c \
        "SELECT count(*) FROM timescaledb_information.hypertables;")
    info "✓ TimescaleDB hypertable 数量: ${HT_COUNT}"
fi

echo ""
info "===== 恢复流程结束 ====="
info "建议在平台页面抽查最近行情与计划数据是否为备份时点状态。"
