# ==========================================================
# BTC 全市场智能研究平台 - 后端镜像（多阶段构建）
# 构建: docker build -t btc-platform-backend ./backend
# ==========================================================

# ---------- Stage 1: 依赖安装（uv + lockfile 冻结安装） ----------
FROM python:3.12-slim AS deps

# uv：极速 Python 包管理器（官方静态二进制，与本地 uv.lock 版本兼容）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# 仅复制依赖定义文件，最大化利用 Docker 层缓存
# 注意：不 COPY README.md —— uv 不构建项目自身（--no-install-project），
# pyproject.toml 中 readme 指向 ../README.md 不会触发读取
COPY pyproject.toml uv.lock ./

# 冻结安装生产依赖到独立虚拟环境（不安装项目自身）
RUN uv venv /app/.venv \
    && UV_PROJECT_ENVIRONMENT=/app/.venv uv sync --frozen --no-dev --no-install-project

# ---------- Stage 2: 运行镜像 ----------
FROM python:3.12-slim AS production

# 运行时系统依赖：curl（HEALTHCHECK 探活）
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# 非 root 运行用户
RUN groupadd -r btc \
    && useradd -r -g btc -d /app -s /usr/sbin/nologin btc

WORKDIR /app

# 复制虚拟环境与应用代码（.dockerignore 已排除 .venv / 缓存 / 日志）
COPY --from=deps /app/.venv /app/.venv
COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts
COPY config ./config
COPY alembic.ini ./

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

USER btc

EXPOSE 8000

# 探活端点：app/main.py 的 /health（进程级健康检查）
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=30s \
    CMD curl -fsS http://localhost:8000/health || exit 1

# uvicorn 多 worker；scheduler 由 compose 以独立命令覆盖启动
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
