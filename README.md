# BTC 全市场智能研究平台

> 面向 BTC 的全市场智能研究、历史数据、周期分析、个人资金计划、策略回测与风险监测一体化平台。

本平台聚合行情、链上、ETF 资金流、衍生品、期权、宏观经济与市场情绪等多源数据，
通过周期定位、估值、风险、市场状态与数据质量等分析引擎，为投资决策提供可解释、
可回测、可监测的研究支撑。

---

## ✨ 核心能力（规划）

- **全市场数据聚合**：行情 / 链上 / ETF / 衍生品 / 期权 / 宏观 / 情绪多源统一接入
- **周期定位**：牛熊阶段识别与周期评分
- **估值分析**：MVRV、NUPL、已实现价格等估值区间判定
- **风险监测**：波动率、回撤、清算风险实时预警
- **策略回测**：历史数据驱动的策略回测框架
- **个人资金计划**：组合管理与定投 / 分批建仓计划

---

## 🛠 技术栈

### 后端
| 类别 | 技术 |
| --- | --- |
| 语言 | Python 3.12+ |
| Web 框架 | FastAPI + Uvicorn |
| ORM / 迁移 | SQLAlchemy 2.0 (async) + Alembic |
| 数据库 | PostgreSQL 16 + TimescaleDB（时序数据） |
| 缓存 | Redis 7 |
| 数据处理 | Polars / NumPy / Pandas |
| 定时任务 | APScheduler |
| 包管理 | uv |

### 前端
| 类别 | 技术 |
| --- | --- |
| 框架 | Next.js 15 + React 19 |
| 语言 | TypeScript |
| 样式 | Tailwind CSS |
| 图表 | ECharts + Lightweight Charts |
| 状态管理 | Zustand |
| HTTP | Axios |
| 包管理 | pnpm |

### 基础设施
- Docker / Docker Compose
- Adminer（开发环境数据库管理 UI）

---

## 🚀 快速开始

### 前置要求

- Python 3.12+ 与 [uv](https://docs.astral.sh/uv/)
- Node.js 20+ 与 pnpm
- Docker 与 Docker Compose

### 1. 配置环境变量

```bash
cp .env.example .env
# 按需修改 .env 中的数据库密码、API Keys 等
```

### 2. 启动基础设施

```bash
# 生产基础（Postgres + Redis）
docker compose up -d

# 开发环境（额外启动 Adminer）
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

### 3. 启动后端

```bash
cd backend
uv sync --extra dev
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

访问 http://localhost:8000/docs 查看 API 文档，http://localhost:8000/health 检查健康状态。

### 4. 启动前端

```bash
cd frontend
pnpm install
pnpm dev
```

访问 http://localhost:3000 。

### 一键启动

- **Linux / macOS**：`./scripts/dev-start.sh`
- **Windows**：`powershell -ExecutionPolicy Bypass -File .\scripts\dev-start.ps1`

或使用 Makefile（需 make）：`make dev-up`、`make backend-dev`、`make frontend-dev`。

---

## 📁 目录结构

```
btc数据分析/
├── backend/                # 后端服务 (FastAPI)
│   ├── app/
│   │   ├── main.py         # 应用入口
│   │   ├── api/v1/         # 版本化路由
│   │   ├── core/           # 配置 / 日志 / 安全
│   │   ├── models/         # ORM 数据模型
│   │   ├── schemas/        # Pydantic 模型
│   │   ├── services/       # 业务服务编排
│   │   ├── providers/      # 外部数据源适配器
│   │   │   ├── base/ market/ onchain/ etf/
│   │   │   └── derivatives/ options/ macro/ sentiment/
│   │   ├── engines/        # 分析引擎
│   │   │   └── cycle/ valuation/ risk/ regime/ quality/
│   │   ├── indicators/     # 技术指标计算
│   │   ├── backtest/       # 策略回测
│   │   ├── portfolio/      # 个人资金计划
│   │   ├── scheduler/      # 定时任务
│   │   └── utils/          # 工具函数
│   ├── migrations/         # Alembic 迁移
│   ├── tests/              # 测试
│   └── scripts/            # 后端脚本
├── frontend/               # 前端 (Next.js)
│   ├── src/
│   │   ├── app/            # App Router 页面
│   │   ├── components/     # 组件
│   │   ├── lib/            # 工具库 / API 客户端
│   │   ├── hooks/          # React Hooks
│   │   ├── stores/         # Zustand 状态
│   │   └── types/          # TypeScript 类型
│   └── public/             # 静态资源
├── docs/architecture/      # 架构文档
├── docker/                 # 容器配置
│   ├── postgres/init.sql   # 数据库初始化
│   └── nginx/              # 反向代理配置
├── scripts/                # 一键启动脚本
├── docker-compose.yml      # 生产基础编排
├── docker-compose.dev.yml  # 开发覆盖编排
├── .env.example            # 环境变量模板
├── Makefile                # 常用命令入口
└── README.md
```

---

## 📝 说明

当前仓库为项目初始化骨架，仅包含目录结构与最小占位代码，业务逻辑将在后续迭代中逐步实现。
