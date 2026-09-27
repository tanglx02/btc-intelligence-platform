#!/usr/bin/env bash
# =============================================================
# uninstall.sh - BTC 平台卸载脚本
# 停止并移除容器；可选删除数据卷（数据库 / Redis / 备份）
# 用法: bash scripts/uninstall.sh
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }

compose() { docker compose -f docker-compose.prod.yml "$@"; }

echo ""
echo -e "${RED}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${RED}║   BTC 平台 - 卸载程序                            ║${NC}"
echo -e "${RED}╚══════════════════════════════════════════════════╝${NC}"
echo ""
warn "此操作将停止并移除所有服务容器（网络将一并移除）"
echo ""

read -r -p "确认卸载? (yes/no): " confirm
if [ "${confirm:-}" != "yes" ]; then
    info "已取消卸载"
    exit 0
fi

# ---------- 1. 停止服务 ----------
echo ""
info "[1/3] 停止并移除所有服务容器..."
compose down --remove-orphans || warn "compose down 出现警告（可能服务本未运行）"

# ---------- 2. 移除 systemd 集成（如已安装） ----------
info "[2/3] 检查 systemd 集成..."
if [ -f /etc/systemd/system/btc-platform.service ]; then
    systemctl stop btc-platform.service 2>/dev/null || true
    systemctl disable btc-platform.service 2>/dev/null || true
    rm -f /etc/systemd/system/btc-platform.service
    systemctl daemon-reload
    info "已移除 systemd 服务 btc-platform.service"
else
    info "未安装 systemd 服务，跳过"
fi

# ---------- 3. 可选：删除数据卷 ----------
info "[3/3] 数据卷处理..."
echo ""
echo -e "${RED}┌──────────────────────────────────────────────────┐${NC}"
echo -e "${RED}│  警告：删除数据卷将永久丢失以下数据，不可恢复！  │${NC}"
echo -e "${RED}│    - PostgreSQL 全部数据（行情/链上/计划/回测）  │${NC}"
echo -e "${RED}│    - Redis 缓存                                  │${NC}"
echo -e "${RED}│    - backups 卷                                  │${NC}"
echo -e "${RED}└──────────────────────────────────────────────────┘${NC}"
echo ""
read -r -p "是否删除所有数据卷? (yes/no): " delete_volumes
if [ "${delete_volumes:-}" = "yes" ]; then
    read -r -p "最终确认：永久删除全部数据（数据库/缓存/备份）? 输入 DELETE-ALL: " final_confirm
    if [ "${final_confirm:-}" = "DELETE-ALL" ]; then
        compose down -v --remove-orphans || true
        echo -e "${RED}所有数据卷已删除${NC}"
    else
        info "输入不匹配，数据卷已保留"
    fi
else
    info "数据卷已保留（可重新安装后继续使用）"
    echo "  如需稍后手动删除: docker compose -f docker-compose.prod.yml down -v"
fi

echo ""
info "配置文件 .env 与宿主机 backups/ 目录未删除，如需彻底清理请手动处理。"
info "卸载完成。"

