# ==========================================================
# BTC 全市场智能研究平台 - 前端镜像（Next.js 15 Standalone 多阶段构建）
# 构建: docker build -t btc-platform-frontend ./frontend
# ==========================================================

# ---------- Stage 1: 依赖安装 ----------
FROM node:20-alpine AS deps

# corepack 启用 pnpm（lockfileVersion 9.0 对应 pnpm 9.x）
RUN corepack enable && corepack prepare pnpm@9 --activate

WORKDIR /app

# 仅复制依赖定义文件，最大化利用 Docker 层缓存
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./

RUN pnpm install --frozen-lockfile

# ---------- Stage 2: 构建 ----------
FROM node:20-alpine AS builder

RUN corepack enable && corepack prepare pnpm@9 --activate

WORKDIR /app

COPY --from=deps /app/node_modules ./node_modules
COPY . .

# 构建期注入前端运行时变量（生产同源走 Nginx 反代，默认相对路径）
ARG NEXT_PUBLIC_API_URL=/api/v1
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL \
    NEXT_TELEMETRY_DISABLED=1

RUN pnpm build

# ---------- Stage 3: 运行镜像（standalone 输出） ----------
FROM node:20-alpine AS production

# 非 root 运行用户
RUN addgroup -S btc && adduser -S btc -G btc

WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# standalone 产物 + 静态资源 + public 目录
COPY --from=builder --chown=btc:btc /app/public ./public
COPY --from=builder --chown=btc:btc /app/.next/standalone ./
COPY --from=builder --chown=btc:btc /app/.next/static ./.next/static

USER btc

EXPOSE 3000

CMD ["node", "server.js"]
