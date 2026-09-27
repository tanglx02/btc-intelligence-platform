#!/usr/bin/env bash
# =============================================================
# manage.sh - BTC 平台交互式管理控制台
# 用法: bash scripts/manage.sh
# =============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

compose() { docker compose -f docker-compose.prod.yml "$@"; }

show_menu() {
    clear 2>/dev/null || true
    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║      BTC 全市场智能研究平台 - 管理控制台         ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  ${GREEN}服务管理${NC}"
    echo "  ─────────────────────────────────────"
    echo "   1) 启动全部服务"
    echo "   2) 停止全部服务"
    echo "   3) 重启全部服务"
    echo "   4) 查看服务状态"
    echo "   5) 查看服务日志"
    echo ""
    echo -e "  ${GREEN}运维操作${NC}"
    echo "  ─────────────────────────────────────"
    echo "   6) 升级平台（update.sh）"
    echo "   7) 备份数据（backup.sh）"
    echo "   8) 恢复备份（restore.sh）"
    echo "   9) 数据库迁移（alembic upgrade head）"
    echo "  10) 测试邮件（SMTP 连通性）"
    echo ""
    echo "  11) 退出"
    echo ""
    read -r -p "  请选择操作 [1-11]: " choice
}

do_logs() {
    echo ""
    echo "  选择服务:"
    echo "   1) backend   2) frontend   3) scheduler"
    echo "   4) postgres  5) nginx"
    read -r -p "  请选择 [1-5]: " svc
    case "$svc" in
        1) compose logs -f --tail=200 backend ;;
        2) compose logs -f --tail=200 frontend ;;
        3) compose logs -f --tail=200 scheduler ;;
        4) compose logs -f --tail=200 postgres ;;
        5) compose logs -f --tail=200 nginx ;;
        *) echo "  无效选择" ;;
    esac
}

do_migrate() {
    compose up -d postgres
    compose run --rm --no-deps backend alembic upgrade head
}

do_test_email() {
    echo ""
    if ! grep -q '^SMTP_ENABLED=true' .env 2>/dev/null; then
        echo -e "${YELLOW}[WARN]${NC} .env 中 SMTP_ENABLED 未开启（当前值: $(grep -E '^SMTP_ENABLED=' .env 2>/dev/null || echo '未设置')）"
        echo "  请先编辑 .env 配置 SMTP_* 变量并设 SMTP_ENABLED=true，然后重启 backend。"
        return 1
    fi
    echo -e "${GREEN}[INFO]${NC} 发送测试邮件（SMTP: $(grep -E '^SMTP_HOST=' .env | cut -d= -f2-)）..."
    compose exec -T backend python - <<'PYEOF'
import asyncio

from email.message import EmailMessage

from app.core.config import settings


async def main() -> None:
    import aiosmtplib

    msg = EmailMessage()
    msg["From"] = f"{settings.smtp_from_name} <{settings.smtp_from_email}>"
    msg["To"] = settings.alert_default_recipient or settings.smtp_user
    msg["Subject"] = "BTC Platform - SMTP 测试邮件"
    msg.set_content("这是一封来自 BTC 平台的测试邮件。收到即表示 SMTP 配置正确。")
    await aiosmtplib.send(
        msg,
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_user or None,
        password=settings.smtp_password or None,
        start_tls=settings.smtp_use_tls and not settings.smtp_use_ssl,
        use_tls=settings.smtp_use_ssl,
        timeout=settings.smtp_timeout_seconds,
    )
    print(f"[INFO] 测试邮件已发送 -> {msg['To']}")

asyncio.run(main())
PYEOF
}

case_handler() {
    case "$1" in
        1)
            echo "启动全部服务..."
            compose up -d
            ;;
        2)
            echo "停止全部服务..."
            compose down
            ;;
        3)
            echo "重启全部服务..."
            compose restart
            ;;
        4)
            compose ps
            echo ""
            docker stats --no-stream \
                $(compose ps -q) 2>/dev/null || true
            ;;
        5) do_logs ;;
        6) bash "$ROOT_DIR/scripts/update.sh" ;;
        7) bash "$ROOT_DIR/scripts/backup.sh" ;;
        8) bash "$ROOT_DIR/scripts/restore.sh" ;;
        9)
            echo "执行数据库迁移..."
            do_migrate
            ;;
        10) do_test_email ;;
        11) echo "再见！"; exit 0 ;;
        *) echo "无效选择" ;;
    esac
}

while true; do
    show_menu
    case_handler "$choice"
    echo ""
    read -r -p "按回车键返回菜单..." _
done
