# ==========================================================
# BTC 全市场智能研究平台 - 常用命令入口
# 使用: make <target>   (Windows 可用 mingw32-make 或 WSL)
# ==========================================================

.DEFAULT_GOAL := help
COMPOSE := docker compose -f docker-compose.yml -f docker-compose.dev.yml

.PHONY: help dev-up dev-down dev-logs backend-install backend-dev frontend-install \
        frontend-dev test lint format migrate backup clean

help: ## 显示可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ---------- Docker / 基础设施 ----------
dev-up: ## 启动开发环境基础设施 (Postgres + Redis + Adminer)
	$(COMPOSE) up -d

dev-down: ## 停止开发环境基础设施
	$(COMPOSE) down

dev-logs: ## 查看基础设施日志
	$(COMPOSE) logs -f

# ---------- 后端 ----------
backend-install: ## 使用 uv 安装后端依赖
	cd backend && uv sync --extra dev

backend-dev: ## 启动后端开发服务器 (热重载)
	cd backend && uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# ---------- 前端 ----------
frontend-install: ## 使用 pnpm 安装前端依赖
	cd frontend && pnpm install

frontend-dev: ## 启动前端开发服务器
	cd frontend && pnpm dev

# ---------- 质量保障 ----------
test: ## 运行后端测试
	cd backend && uv run pytest -v

lint: ## 代码检查 (ruff + mypy)
	cd backend && uv run ruff check . && uv run mypy app
	cd frontend && pnpm lint

format: ## 代码格式化
	cd backend && uv run ruff format . && uv run ruff check --fix .

# ---------- 数据库 ----------
migrate: ## 执行 Alembic 数据库迁移
	cd backend && uv run alembic upgrade head

backup: ## 备份数据库到 ./backups
	@mkdir -p backups
	docker exec btc_postgres pg_dump -U $${POSTGRES_USER:-btc_admin} $${POSTGRES_DB:-btc_platform} \
		> backups/backup_$$(date +%Y%m%d_%H%M%S).sql

clean: ## 清理缓存与构建产物
	find . -type d -name __pycache__ -exec rm -rf {} +
	rm -rf backend/.pytest_cache backend/.mypy_cache backend/.ruff_cache
	rm -rf frontend/.next
