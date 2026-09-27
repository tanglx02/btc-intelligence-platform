#!/usr/bin/env bash
# =============================================================
# backup.sh - BTC 平台备份脚本
# 内容: PostgreSQL 全量 (pg_dump -Fc) + .env + providers.yaml
# 保留: 每日备份保留 30 天（超期自动清理）
# 定时: crontab -e -> 0 3 * * * cd /opt/btc-platform && bash scripts/backup.sh >> backups/backup.log 2>&1
# 用法: bash scripts/backup.sh
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

# ---------- 从 .env 读取配置 ----------
[ -f .env ] || fail "未找到 .env，请先运行 install.sh"
env_value() {
    grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- | sed 's/^["'\'']//; s/["'\'']$//'
}

PG_USER="$(env_value POSTGRES_USER)"
PG_DB="$(env_value POSTGRES_DB)"
BACKUP_ENCRYPTION_KEY="$(env_value BACKUP_ENCRYPTION_KEY)"
PG_USER="${PG_USER:-btc_admin}"
PG_DB="${PG_DB:-btc_platform}"

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="$ROOT_DIR/backups"
mkdir -p "$BACKUP_DIR/config"

echo ""
info "===== BTC 平台备份开始（${TIMESTAMP}） ====="

# ---------- 1. 数据库全量备份 ----------
DUMP_FILE="$BACKUP_DIR/btc_platform_${TIMESTAMP}.dump"
TMP_FILE="${DUMP_FILE}.tmp"
info "[1/3] 导出数据库 ${PG_DB} (pg_dump 自定义格式, -Fc 内置压缩)..."
# 写临时文件成功后原子重命名，避免中途失败留下半截备份
if compose exec -T postgres pg_dump -U "$PG_USER" -Fc "$PG_DB" > "$TMP_FILE"; then
    mv "$TMP_FILE" "$DUMP_FILE"
else
    rm -f "$TMP_FILE"
    fail "pg_dump 失败，请确认 postgres 容器运行中（bash scripts/manage.sh 选 4 查看状态）"
fi

# ---------- 2. 配置文件备份 ----------
info "[2/3] 备份配置文件（.env / providers.yaml）..."
if [ -n "$BACKUP_ENCRYPTION_KEY" ] && command -v openssl >/dev/null 2>&1; then
    openssl enc -aes-256-cbc -salt -pbkdf2 \
        -in .env \
        -out "$BACKUP_DIR/config/env_${TIMESTAMP}.enc" \
        -pass "pass:${BACKUP_ENCRYPTION_KEY}" 2>/dev/null
    info "  .env 已加密备份: config/env_${TIMESTAMP}.enc"
else
    cp .env "$BACKUP_DIR/config/env_${TIMESTAMP}.conf"
    chmod 600 "$BACKUP_DIR/config/env_${TIMESTAMP}.conf"
    warn "  .env 为明文备份（config/env_${TIMESTAMP}.conf，已限 600 权限）"
    warn "  建议在 .env 中设置 BACKUP_ENCRYPTION_KEY 以启用加密备份"
fi

if [ -f backend/config/providers.yaml ]; then
    cp backend/config/providers.yaml "$BACKUP_DIR/config/providers_${TIMESTAMP}.yaml"
    info "  providers.yaml 已备份: config/providers_${TIMESTAMP}.yaml"
else
    warn "  未找到 backend/config/providers.yaml，跳过"
fi

# ---------- 3. 保留策略清理 ----------
info "[3/3] 清理 30 天前的旧备份..."
DELETED=$(find "$BACKUP_DIR" -maxdepth 1 -name 'btc_platform_*.dump' -mtime +30 -print -delete | wc -l)
find "$BACKUP_DIR/config" -type f -mtime +30 -delete 2>/dev/null || true
info "  已清理 ${DELETED} 份数据库备份"

# ---------- 汇总输出 ----------
echo ""
info "===== 备份完成 ====="
DUMP_SIZE=$(du -h "$DUMP_FILE" | cut -f1)
info "数据库备份: $DUMP_FILE (${DUMP_SIZE})"
info "配置备份目录: $BACKUP_DIR/config/"
echo ""
echo "最近 5 份备份:"
ls -lht "$BACKUP_DIR"/btc_platform_*.dump 2>/dev/null | head -n 5 | awk '{printf "  %s  %s\n", $5, $9}'
