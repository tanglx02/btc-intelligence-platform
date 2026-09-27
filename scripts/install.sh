#!/usr/bin/env bash
# =============================================================
# install.sh - BTC 全市场智能研究平台 一键安装
# 支持: Ubuntu 20.04+ / Debian 11+（其他发行版需自备 Docker）
# 用法: sudo bash scripts/install.sh
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

# ---------- 输出工具 ----------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail() { echo -e "${RED}[ERROR]${NC} $1" >&2; exit 1; }

compose() { docker compose -f docker-compose.prod.yml "$@"; }

# 等待容器 healthy（默认上限 180s）
wait_healthy() {
    local container="$1" timeout="${2:-180}" elapsed=0
    until [ "$(docker inspect -f '{{.State.Health.Status}}' "$container" 2>/dev/null || echo starting)" = "healthy" ]; do
        elapsed=$((elapsed + 2))
        if [ "$elapsed" -ge "$timeout" ]; then
            fail "等待 $container 就绪超时（${timeout}s）。请检查日志: bash scripts/manage.sh（选 5 查看日志）"
        fi
        sleep 2
    done
}

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║   BTC 全市场智能研究平台 - 一键安装程序           ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════════╝${NC}"
echo ""

# ============ [1/7] 依赖检查 ============
info "[1/7] 检查系统依赖..."

if [ "$(id -u)" -ne 0 ]; then
    warn "当前非 root 用户，需确保已加入 docker 组（否则后续步骤将失败）"
fi

if command -v docker >/dev/null 2>&1; then
    info "Docker 已安装: $(docker --version)"
else
    warn "未检测到 Docker"
    if [ -r /etc/os-release ] && grep -qiE 'ubuntu|debian' /etc/os-release && command -v curl >/dev/null 2>&1; then
        read -r -p "是否自动安装 Docker（官方脚本 get.docker.com）? [y/N]: " install_docker
        if [[ "${install_docker:-n}" =~ ^[Yy]$ ]]; then
            info "正在安装 Docker（约 1-3 分钟）..."
            curl -fsSL https://get.docker.com | sh
            systemctl enable --now docker
            info "Docker 安装完成: $(docker --version)"
        else
            fail "请先安装 Docker: https://docs.docker.com/engine/install/"
        fi
    else
        fail "请先安装 Docker 与 Docker Compose V2 后重试"
    fi
fi

docker info >/dev/null 2>&1 || fail "Docker 守护进程未运行，请执行: sudo systemctl start docker"
docker compose version >/dev/null 2>&1 || fail "Docker Compose V2 未安装（缺少 docker compose 子命令）"
info "Docker Compose: $(docker compose version --short)"

# ============ [2/7] 环境配置 ============
info "[2/7] 初始化环境配置..."
if [ ! -f .env ]; then
    cp .env.example .env
    if command -v openssl >/dev/null 2>&1; then
        DB_PASS="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"
        REDIS_PASS="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"
        SECRET_KEY_VAL="$(openssl rand -hex 32)"
        sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${DB_PASS}|" .env
        sed -i "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=${REDIS_PASS}|" .env
        sed -i "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET_KEY_VAL}|" .env
        sed -i "s|^APP_ENV=.*|APP_ENV=production|" .env
        sed -i "s|^DEBUG=.*|DEBUG=false|" .env
        info "已生成随机数据库/Redis 密码与 JWT 密钥（保存于 .env，请妥善保管）"
    else
        warn "未找到 openssl，保留默认密码 —— 请立即编辑 .env 修改 POSTGRES_PASSWORD / REDIS_PASSWORD / SECRET_KEY！"
    fi
    chmod 600 .env
else
    warn "已存在 .env，沿用现有配置"
    if grep -q '^POSTGRES_PASSWORD=changeme' .env; then
        warn ".env 中数据库密码仍为默认值，强烈建议修改！"
    fi
fi
mkdir -p backups docker/nginx/ssl

# ============ [3/7] 构建镜像 ============
info "[3/7] 构建 Docker 镜像（backend / frontend，首次约 5-15 分钟）..."
compose build

# ============ [4/7] 启动基础设施 ============
info "[4/7] 启动数据库与缓存..."
compose up -d postgres redis
wait_healthy btc_postgres 300
wait_healthy btc_redis 60
info "PostgreSQL 与 Redis 已就绪"

# ============ [5/7] 数据库迁移 ============
info "[5/7] 执行数据库迁移（Alembic）..."
# 一次性容器内执行（--no-deps 不触发依赖重启），等价于在 backend 容器内运行
compose run --rm --no-deps backend alembic upgrade head

# ============ [6/7] 种子数据 ============
info "[6/7] 初始化种子数据（assets / providers / indicators / events）..."
compose run --rm --no-deps backend python scripts/seed_data.py

# ============ [7/7] 启动全部服务 ============
info "[7/7] 启动全部服务..."
compose up -d
sleep 5
compose ps

SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")"
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   安装完成！                                     ║${NC}"
echo -e "${GREEN}╠══════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║${NC}  平台入口   : http://${SERVER_IP}/"
echo -e "${GREEN}║${NC}  API 服务   : http://${SERVER_IP}/api/v1"
echo -e "${GREEN}║${NC}  健康检查   : http://${SERVER_IP}/health"
echo -e "${GREEN}║${NC}  管理控制台 : bash scripts/manage.sh"
echo -e "${GREEN}║${NC}  数据库账号 : 见 .env（POSTGRES_USER / POSTGRES_PASSWORD）"
echo -e "${GREEN}║${NC}  预警收件人 : 编辑 .env 中 ALERT_DEFAULT_RECIPIENT 后"
echo -e "${GREEN}║${NC}               执行 bash scripts/manage.sh（选 3 重启）生效"
echo -e "${GREEN}╚══════════════════════════════════════════════════╝${NC}"
echo ""
info "后续建议:"
echo "  1. 编辑 .env 填写数据源 API Keys（PROVIDER_*_KEY）"
echo "  2. 配置 SMTP 邮件告警（SMTP_* 与 ALERT_DEFAULT_RECIPIENT）"
echo "  3. 设置每日自动备份: crontab -e 添加"
echo "     0 3 * * * cd $(pwd) && bash scripts/backup.sh >> backups/backup.log 2>&1"
