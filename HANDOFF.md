# BTC 全市场智能研究平台 — 开发交接文档

> 交接日期：2026-09-27 ｜ 交接目标：下一个 AI 开发工具可直接接手继续开发与 Bug 修复
> GitHub 仓库：https://github.com/tanglx02/btc-intelligence-platform （public，main 分支）
> 本地代码：`d:\Project\项目开发\btc数据分析`

---

## 一、项目是什么

从 0 到 1 开发的 BTC 全市场智能研究平台（非行情网站）：多数据源采集 → 数据质量保障 → 指标计算 → 四大分析引擎（周期/估值/风险/综合状态）→ 回测与定投模拟 → 智能邮件预警。面向非金融用户（默认简洁模式）和专业用户（专业模式），所有结论可解释、数据可溯源。

**核心设计红线**（修改代码时必须遵守）：
- 绝不生成假数据；数据不可用时降级显示"数据暂不可用"，不编造
- 回测严格 Point-in-Time 防未来数据泄漏（宏观数据用 release_date 而非 observation_date）
- 告警三态求值（TRUE/FALSE/UNKNOWN），数据 stale/缺失 → UNKNOWN 不触发
- 局部故障不影响整体（任何 Provider/模块挂掉系统降级运行）
- 引擎输出禁止 BUY/SELL 指令性建议，只做描述性判断并附证据链

## 二、技术栈与架构

| 层 | 技术 |
|---|---|
| 后端 | Python 3.12 + FastAPI + SQLAlchemy 2.0 (async) + Alembic + loguru |
| 数据库 | PostgreSQL 16 + TimescaleDB（迁移未强依赖 timescaledb 扩展，普通 PG 可跑） |
| 缓存 | Redis 7/8（多数组件有内存降级方案，Redis 挂了系统仍可跑） |
| 前端 | Next.js 15 App Router + React 19 + TS + Tailwind（暗色主题）+ ECharts + TradingView LW Charts + Zustand |
| 调度 | 自研 asyncio SchedulerService（**不要引入 APScheduler**，历史决策） |
| 部署 | Docker Compose（生产，本机未装 Docker 用原生部署） |

**分层**：Provider 层（21个数据源，优先级队列+故障切换+健康评分）→ Service 层（本地DB优先→Redis缓存→ProviderManager）→ Engine 层 → API 层（76 路由）→ 前端。
**关键机制文档**（改代码前必读对应章节）：`docs/architecture/` 下 23 份文档，重点：04 Provider架构、05 故障切换、09 核心表DDL、10 数据质量、12 指标字典、13 模型架构、17 API设计、23 开发路线。

## 三、本机运行状态（Windows 10 22H2，管理员）

| 服务 | 方式 | 端口 | 说明 |
|---|---|---|---|
| PostgreSQL 16 | **Windows 服务 `pgsql-btc`**（开机自启） | 127.0.0.1:5432 | 库 btc_platform，用户 postgres，trust 认证（无密码） |
| Redis 8 | 后台进程 | 127.0.0.1:6379 | 密码 changeme |
| 后端 uvicorn | 后台终端 | 127.0.0.1:8000 | 无 --reload，改代码需手动重启 |
| 前端 next dev | 后台终端 | 3000 | |

- `.env` 在**两处**：项目根目录 + `backend/.env`（config 的 env_file 相对 CWD；两份内容需保持一致，改配置两边都改）
- 数据源代理：SOCKS5（凭据在 .env 的 `PROXY_SOCKS5_URL`），已写入 providers 表（DB 为运行时权威）；本地代理 `127.0.0.1:7890`（HTTP）在 GitHub/Google 访问时更快
- 安装位置：PostgreSQL/工具在 `D:\btc-local-deploy\`（仓库外）
- **重启命令**：
  - 后端：`cd backend ; .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000`（后台运行）
  - 前端：`cd frontend ; pnpm dev`
  - Redis：`D:\btc-local-deploy\redis8\redis-server.exe`（若停了）

## 四、常用命令

```powershell
# 全量测试（基线 453 passed，改代码后必须保持全绿）
cd backend ; .\.venv\Scripts\python.exe -m pytest tests/ -q

# 数据库迁移（当前 head=003）
cd backend ; .\.venv\Scripts\python.exe -m alembic upgrade head

# 种子数据
.\.venv\Scripts\python.exe scripts\seed_data.py

# 历史K线回填（Binance 分页拉取）
.\.venv\Scripts\python.exe scripts\backfill_history.py

# 前端构建验证
cd frontend ; pnpm build

# Lint（Python）
.\.venv\Scripts\python.exe -m ruff check app/
```

## 五、已完成内容（勿重复开发）

- **数据库**：52+4 张表 ORM + 迁移 001~003（003 含 system_settings 配置中心表）
- **Provider 体系**：8 大类 21+ Provider（binance/okx/coinbase/bybit/kraken/deribit/alternative_me/blockchain_com/world_bank/farside 已通，其余等 API Key）；健康评分 8 维度、故障切换、Recovery Threshold、防抖动、热重载（改 DB 配置即生效，无需重启）
- **数据管线**：Raw/Normalized 双存储、断点续传、自动补洞、多源交叉验证（价格偏差>0.5% 标 CONFLICT）
- **指标**：28 个（技术11/链上8/衍生品3/复合6），Polars 实现
- **引擎**：Cycle（9阶段防抖状态机）、Valuation（5级）、Risk（11因子→7维度）、MarketRegime（9维融合）
- **回测**：事件驱动+向量化双引擎、PIT 防泄漏、6 种定投模拟、历史回放双视角
- **预警系统**：条件规则引擎（阈值/变化率/区间/交叉/分位/状态变化/AND-OR-NOT嵌套/持续时间/连续次数）、Cooldown、SMTP 邮件（HTML模板含完整上下文）、每日摘要/周报、12 预设模板、历史触发模拟
- **配置后台化**：system_settings + Fernet 加密 + PUT /providers/{name} 热重载，全部配置可在 /providers 和 /admin 页面改
- **前端**：25 页面全部完成（暗色主题、普通/专业双模式、三级溯源、WebSocket 实时价格）
- **API**：76 路由 + WebSocket（/api/v1/ws/market）
- **部署**：docker-compose.prod.yml + Dockerfile×2 + nginx + systemd + install/update/uninstall/backup/restore/manage 脚本
- **测试**：453 项（indicators 74 / portfolio+backtest 53 / integration 292+34）
- **README**：已含 14 张真实截图（docs/screenshots/），LICENSE MIT

## 六、已知问题与建议待办（按优先级）

1. **首页 dashboard 聚合接口部分维度为 null**：`/api/v1/engine/dashboard` 中 cycle/valuation 等字段为 null（对应独立页面 /cycle /valuation 数据正常）。排查 engines 聚合端点取数逻辑——大概率是 dashboard 端点读的是 `get_latest()` 而引擎结果写库时间/查询条件不匹配。这是用户可见度最高的待修项。
2. **7 个数据源等 API Key**：glassnode/fred/cryptoquant/coinglass/coinglass_options/lunar_crush/sosovalue。用户在 /providers 页「配置」填入 Key 后点重载即生效（架构已就绪，无需改代码）。链上/宏观维度因此权重降级。
3. **3 个 Provider 未实现实现类**：providers.yaml 中 bybit_derivatives、okx_options、intotheblock 有配置无实现（启动时安全跳过）。
4. **okx 健康检查经代理返回 404**（代理出口返回 HTML），okx 偶发转 offline 后 health_monitor 60s 轮转自愈；可考虑给 okx 的 health_check 换更轻量的端点。
5. **Alert 引擎尚未接入 Scheduler 的 alert_maintenance_task**（重试失败投递+摘要定时发送的函数已提供在 alerts/dispatcher.py，scheduler/jobs.py 未注册它）。
6. **认证系统为可选骨架**：get_current_user 无 token 返回 None 不强制；预警规则是系统级（user_id NULL）。按需实现 JWT 完整登录（core/security.py 已有哈希和 token 函数）。
7. **性能观察项**：datetime.utcnow 已全量替换为 aware（app/utils/datetime_utils.py），DB 读回 naive 时用 as_naive_utc 归一——新增代码注意 naive/aware 比较陷阱（历史上炸过 3 次）。
8. **crypto 层**：proxy_config 在 DB 是明文（加密层 Fernet 已建，api_key 走加密；proxy 未接解密读取），如需统一可扩展。

## 七、踩坑记录（新 AI 必读，避免重复踩坑）

1. **PowerShell 5.1**：不支持 `&&`，用 `;`；管道传 JSON 给 python -c 会崩，复杂逻辑写临时 .py 文件执行后删除。
2. **前台命令启动的子进程会被会话清理**：长驻服务必须用后台终端，或注册 Windows 服务（pg_ctl register -N 名称 -S auto）。
3. **长驻进程跑旧代码**：改代码后接口 404，先对比运行中 /openapi.json 路由数确认是旧进程，重启而非改代码。8000 端口可能被**其他项目**（D:\Project\项目开发\btc数据监测网站）占用，重启前 netstat 确认。
4. **naive/aware datetime**：统一 aware（utcnow helper），与 DB/Provider 内部 naive 值比较时归一化（_as_utc/as_naive_utc）。
5. **Polars**：NaN≠null，Wilder 平滑前导用 null 不能用 NaN（否则 ewm_mean(ignore_nulls) 失效全 NaN）；min_periods 已更名 min_samples。
6. **Provider 类名含大写缩写**（OKXProvider→o_k_x）需显式 provider_key 对齐 YAML/DB key。
7. **SQLAlchemy on_conflict_do_update 参数是 index_elements**（不是 index_element，写错过一次）。
8. **ENUM 建约定**：模型层 SAEnum(create_type=False)，迁移中 sa.Enum(..., create_type=True).create(checkfirst=True)；ENUM Python 成员名≠存储值时用 values_callable。
9. **alembic.ini 含中文会 GBK 解码崩**，配置文件注释写英文。
10. **下载被墙**：GitHub/Google 用 curl.exe -x "socks5h://..." 或本地代理 7890（或 npmmirror 镜像 + PLAYWRIGHT_DOWNLOAD_HOST）；大文件加 -C - 断点续传。
11. **截图/浏览器自动化**：playwright MCP 硬编码 channel=chrome；无 Chrome 时用 playwright-core + npmmirror chromium + Node 脚本（参考 D:\btc-local-deploy\shot\shot.js）。
12. **前端 Unified 响应**：所有 API 返回 {success, data, meta, error} 信封（backend/app/schemas/base.py 的 ok()/err()）；路由端点返回注解**禁止写联合类型**（如 `-> dict | JSONResponse` 会破坏 FastAPI 挂载）。

## 八、Git 工作流

- 远程推送需代理：`git -c http.proxy="http://127.0.0.1:7890" push origin main`
- GitHub token 在用户侧（过期则让用户提供新的 PAT，scope=repo）；本地 .git/config 无 token 痕迹（保持干净，不要把 token 写进跟踪文件）
- .env / backend/.env 均已 gitignore；提交前 `git grep <敏感串>` 扫描
- 提交规范：feat/fix/docs/test + scope，单任务单提交

## 九、验证清单（接手后先跑一遍确认环境正常）

```powershell
curl http://127.0.0.1:8000/health                                  # 200 {"status":"ok"}
curl http://127.0.0.1:8000/api/v1/market/price                     # 200 真实价格
curl http://127.0.0.1:8000/api/v1/engine/dashboard                 # 200（维度字段可能部分 null，见待办1）
curl http://127.0.0.1:8000/api/v1/sentiment/fear-greed             # 200 value=70
curl http://127.0.0.1:8000/api/v1/providers                        # 200 24个，10个在线
cd backend ; .\.venv\Scripts\python.exe -m pytest tests/ -q        # 453 passed
curl http://localhost:3000                                         # 200 HTML
```
