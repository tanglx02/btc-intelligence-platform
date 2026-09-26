# 18 - 权限设计（Permission Design）

> BTC 全市场智能研究平台 · 认证、授权与审计设计
>
> 技术栈：FastAPI + JWT（python-jose）+ Fernet（cryptography）+ PostgreSQL + Redis

---

## 概述

平台面向三类使用者：系统管理员（管理 Provider、任务、备份）、分析师（策略/模型研究）、普通用户（个人计划与市场查看），并允许匿名浏览公开市场数据。权限设计目标：

1. **最小权限**：每个角色只获得完成其职责所需的权限；个人数据严格隔离；
2. **密钥安全**：Provider API Key 使用 Fernet 对称加密存储，任何界面/日志不回显明文；
3. **完整审计**：所有写操作与敏感读操作记录审计日志，可追溯到人；
4. **降级安全**：数据源故障、降级运行不改变权限边界。

---

## 目录

1. [RBAC 模型](#1-rbac-模型)
2. [完整权限矩阵](#2-完整权限矩阵)
3. [JWT 认证流程](#3-jwt-认证流程)
4. [API Key 管理（Fernet 加密）](#4-api-key-管理fernet-加密)
5. [操作审计日志](#5-操作审计日志)
6. [资源级访问控制](#6-资源级访问控制)
7. [安全防护](#7-安全防护)

---

## 1. RBAC 模型

### 1.1 角色定义

| 角色 | 代码 | 定位 | 典型用户 |
|------|------|------|---------|
| 管理员 | `admin` | 系统全权：Provider 管理、任务控制、用户管理、备份恢复、模型激活 | 平台拥有者（本人） |
| 分析师 | `analyst` | 研究权限：策略/模型实验室、Provider 只读健康数据、全量回测查看 | 深度研究场景的第二身份 |
| 普通用户 | `user` | 个人空间：计划、资产、定投模拟、回测、AI 助手；公开市场数据 | 日常使用 |
| 匿名 | `anonymous` | 只读公开市场数据（无个人数据、无重操作） | 未登录浏览 |

角色为**单角色模型**（一个用户同一时刻一个角色），继承关系：`admin ⊃ analyst ⊃ user ⊃ anonymous`（高角色自动包含低角色全部权限）。

> **大小写约定**：数据库 ENUM `user_role` 存储大写值（ADMIN/ANALYST/USER，详见 09-core-tables.md）；应用层代码、JWT payload 及路由守卫使用小写（admin/analyst/user）；`anonymous` 为应用层隐含角色（未登录用户），不入库。ORM 层自动完成大小写转换。

### 1.2 数据模型

```sql
-- 用户表
CREATE TABLE users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username      VARCHAR(64) UNIQUE NOT NULL,
    email         VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,          -- argon2id
    role          user_role NOT NULL DEFAULT 'USER',  -- ENUM: ADMIN/ANALYST/USER（详见 09-core-tables.md）
    status        VARCHAR(16) NOT NULL DEFAULT 'active',-- active/disabled
    preferences   JSONB NOT NULL DEFAULT '{}',    -- 普通/专业模式、涨跌颜色等
    failed_login_count INT NOT NULL DEFAULT 0,
    locked_until  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 刷新令牌表（支持吊销与旋转）
CREATE TABLE refresh_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  VARCHAR(255) NOT NULL UNIQUE,     -- 只存 SHA-256 哈希
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ,
    replaced_by UUID,                             -- 旋转链，检测重放
    user_agent  TEXT,
    ip          INET,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 审计日志表（见第 5 节）
CREATE TABLE audit_logs ( ... );
```

### 1.3 权限判定实现

FastAPI 依赖注入三层守卫：

```text
get_current_user        → 解析 JWT，得到 user 或 anonymous（公开端点）
require_role("admin")   → 角色校验，不足返回 40301
require_owner(getter)   → 资源属主校验（如计划、回测），非本人返回 40403（用 404 语义防资源探测）
```

写权限装饰器同时触发审计日志中间件（第 5 节）。

---

## 2. 完整权限矩阵

> 图例：✅ 允许 | ❌ 禁止 | 👁 只读 | 本人 = 仅访问自己创建的资源。
> 端点与《17-API 设计》第 3 节一一对应。

### 2.1 认证与个人数据

| 端点 | 匿名 | User | Analyst | Admin |
|------|:----:|:----:|:-------:|:-----:|
| POST /auth/register | ✅ | — | — | — |
| POST /auth/login / refresh | ✅ | ✅ | ✅ | ✅ |
| GET/PUT /auth/me | ❌ | ✅(本人) | ✅(本人) | ✅(本人) |
| PUT /auth/me/password | ❌ | ✅(本人) | ✅(本人) | ✅(本人) |
| POST /auth/logout | ❌ | ✅ | ✅ | ✅ |

### 2.2 市场与数据查看（公开只读）

| 端点组 | 匿名 | User | Analyst | Admin |
|--------|:----:|:----:|:-------:|:-----:|
| /market/* | ✅ | ✅ | ✅ | ✅ |
| /onchain/* | ✅ | ✅ | ✅ | ✅ |
| /etf/* | ✅ | ✅ | ✅ | ✅ |
| /derivatives/* | ✅ | ✅ | ✅ | ✅ |
| /options/* | ✅ | ✅ | ✅ | ✅ |
| /macro/* | ✅ | ✅ | ✅ | ✅ |
| /sentiment/* | ✅ | ✅ | ✅ | ✅ |
| /indicators/* | ✅ | ✅ | ✅ | ✅ |
| /engine/*（含 explain/history） | ✅ | ✅ | ✅ | ✅ |
| GET /system/health、/system/ready | ✅ | ✅ | ✅ | ✅ |
| GET /system/data-quality（摘要） | ✅ | ✅ | ✅完整 | ✅完整 |
| GET /system/data-quality/conflicts | ❌ | ❌ | ✅ | ✅ |
| GET /providers（摘要） | ✅ | ✅ | ✅完整 | ✅完整 |

### 2.3 个人资产（严格属主隔离）

| 端点 | 匿名 | User | Analyst | Admin |
|------|:----:|:----:|:-------:|:-----:|
| CRUD /portfolio/plans | ❌ | ✅(本人) | ✅(本人) | ✅(本人)¹ |
| CRUD /portfolio/transactions | ❌ | ✅(本人) | ✅(本人) | ✅(本人)¹ |
| GET /portfolio/holdings、/performance | ❌ | ✅(本人) | ✅(本人) | ✅(本人)¹ |
| POST /portfolio/simulate | ❌ | ✅ | ✅ | ✅ |
| GET /portfolio/export | ❌ | ✅(本人) | ✅(本人) | ✅(本人)¹ |

¹ Admin **不可**浏览其他用户的个人资产数据（隐私红线）；仅在用户本人发起支持请求且系统提供专门授权流程时例外（预留，默认关闭）。Admin 自己的账户按本人规则访问。

### 2.4 回测与研究

| 端点 | 匿名 | User | Analyst | Admin |
|------|:----:|:----:|:-------:|:-----:|
| POST /backtest/run | ❌ | ✅ | ✅ | ✅ |
| GET /backtest/runs | ❌ | 👁(本人) | 👁(全部) | 👁(全部) |
| GET /backtest/runs/{id} | ❌ | 👁(本人) | 👁(全部) | 👁(全部) |
| DELETE /backtest/runs/{id} | ❌ | ✅(本人) | ❌ | ✅(全部) |
| POST /backtest/compare | ❌ | ✅(本人run) | ✅ | ✅ |
| GET/POST /strategies/* | ❌ | ❌ | ✅ | ✅ |
| POST /strategies/{id}/validate | ❌ | ❌ | ✅ | ✅ |
| GET /models、/models/{id}、/models/{id}/performance | ❌ | ❌ | ✅ | ✅ |
| POST /models/{id}/activate | ❌ | ❌ | ❌ | ✅ |
| POST /ai/chat、会话管理 | ❌ | ✅(本人) | ✅(本人) | ✅(本人) |

### 2.5 Provider 与系统管理

| 端点 | 匿名 | User | Analyst | Admin |
|------|:----:|:----:|:-------:|:-----:|
| GET /providers/{id}、/providers/{id}/health | ❌ | ❌ | ✅ | ✅ |
| GET /providers/failover-events | ❌ | ❌ | ✅ | ✅ |
| POST /providers/{id}/test、/providers/test-all | ❌ | ❌ | ❌ | ✅ |
| POST /providers（新增）、DELETE /providers/{id} | ❌ | ❌ | ❌ | ✅ |
| PUT /providers/{id}/config、/priority-lock | ❌ | ❌ | ❌ | ✅ |
| GET /providers/{id}/requests（请求日志） | ❌ | ❌ | ❌ | ✅ |
| GET /system/jobs*（列表/详情） | ❌ | ❌ | 👁 | ✅ |
| POST /system/jobs/{id}/run、pause、resume、retry、skip | ❌ | ❌ | ❌ | ✅ |
| GET /system/stats、/system/audit-logs | ❌ | ❌ | ❌ | ✅ |
| POST /system/backup、GET /system/backups、POST /system/backups/{id}/restore | ❌ | ❌ | ❌ | ✅ |
| 用户管理（列表/改角色/禁用/重置密码） | ❌ | ❌ | ❌ | ✅ |
| WS job.logs 频道 | ❌ | ❌ | ❌ | ✅ |

### 2.6 页面访问矩阵（前端路由守卫）

| 页面 | 匿名 | User | Analyst | Admin |
|------|:----:|:----:|:-------:|:-----:|
| 首页及全部市场页面（行情/周期/估值/链上/资金流/ETF/衍生品/Options/宏观/情绪/风险/历史回放） | ✅ | ✅ | ✅ | ✅ |
| 数据质量页 | ✅(摘要) | ✅(摘要) | ✅ | ✅ |
| 我的计划/我的资产/定投模拟 | ❌(引导登录) | ✅ | ✅ | ✅ |
| 策略回测 | ❌(引导登录) | ✅ | ✅ | ✅ |
| AI 研究助手 | ❌(引导登录) | ✅ | ✅ | ✅ |
| 策略实验室/模型实验室 | ❌ | ❌ | ✅ | ✅ |
| 数据源中心 | ❌ | 👁(简化只读) | ✅ | ✅(含管理操作) |
| 系统任务 | ❌ | ❌ | 👁 | ✅ |
| 后台管理 | ❌ | ❌ | ❌ | ✅ |

前端守卫规则：路由级 middleware 校验角色，无权访问时跳转登录页或 403 页；**前端守卫仅做体验优化，后端 API 权限校验才是安全边界**。

---

## 3. JWT 认证流程

### 3.1 令牌策略

| 令牌 | 有效期 | 存储（前端） | 说明 |
|------|--------|-------------|------|
| access_token | **30 分钟** | 内存（Zustand store，不落 localStorage） | HS256 签名；载荷含 `sub`(user_id)、`role`、`exp`、`iat`、`jti` |
| refresh_token | **7 天** | httpOnly + Secure + SameSite=Strict Cookie（或加密存储回退） | 数据库存 SHA-256 哈希；**旋转机制**：每次刷新签发新 refresh_token 并作废旧的 |

access_token 载荷示例：

```json
{
  "sub": "u_8f3a2b1c",
  "role": "user",
  "username": "alice",
  "iat": 1790446773,
  "exp": 1790448573,
  "jti": "t_9d2e1f4a"
}
```

### 3.2 完整流程

```text
① 登录
   Client ──POST /auth/login {username, password}──▶ Server
   Server: argon2id 校验 → 签发 access(30min) + refresh(7d)
           refresh 哈希写入 refresh_tokens 表
           失败计数 +1（连续 5 次失败锁定账号 15 分钟，写审计）

② 携带访问
   Client ──Authorization: Bearer <access_token>──▶ 受保护端点
   Server: 验签 → 校验 exp/jti 吊销名单(Redis) → 角色/属主守卫

③ 静默刷新（access 过期）
   Client 收到 401(40102) → POST /auth/refresh {refresh_token}
   Server: 校验哈希存在且未吊销未过期
           → 旧 token 标记 revoked_at，replaced_by 指向新 token
           → 返回新 access + 新 refresh（旋转）
   若旧 refresh 已被使用过（重放检测）→ 吊销整条 token 链 + 告警 + 审计

④ 登出 / 吊销
   POST /auth/logout → refresh 标记 revoked；access 的 jti 加入 Redis 黑名单（TTL=剩余有效期）
   Admin 禁用用户 → 该用户全部 refresh 吊销 + jti 黑名单

⑤ WebSocket 认证
   连接时 ?token=<access_token> 校验；access 过期后服务端发送
   {"action":"auth_required"}，客户端须在 30s 内发 {"action":"auth","token":...}，否则断开
```

### 3.3 安全要求

- 签名密钥 `SECRET_KEY` 仅从环境变量注入（生产用独立随机 256bit，定期轮换，轮换期支持双密钥验签）；
- 密码哈希：argon2id（memory≥64MB, iterations≥3）；密码策略 ≥ 10 位含字母数字；
- 全站 HTTPS；HSTS；Cookie 加 `Secure` + `SameSite=Strict`；
- 登录/刷新端点独立严格限流（10 req/min/IP），防暴力破解；
- 时钟偏移容忍 ≤ 60s（`leeway`）。

---

## 4. API Key 管理（Fernet 加密）

### 4.1 加密方案

Provider 的 API Key / Secret 属于最高敏感数据，采用 **Fernet 对称加密**（AES-128-CBC + HMAC-SHA256，`cryptography` 库）存储：

```text
主密钥 MASTER_KEY（32 字节 urlsafe base64）
  ├── 仅从环境变量 / 独立密钥文件注入，绝不入库、绝不入 Git
  ├── 与数据库物理分离（数据库备份泄露 ≠ 密钥泄露）
  └── 支持轮换：轮换时用旧钥解密→新钥加密→更新 key_version 字段

providers 表相关字段：
  api_key_encrypted    BYTEA   -- Fernet token
  api_key_hint         VARCHAR(16)  -- 掩码提示 "sk-****abcd"（仅尾4位）
  key_version          INT     -- 加密密钥版本号
  key_updated_at       TIMESTAMPTZ
  key_updated_by       UUID    -- 操作人（审计关联）
```

### 4.2 使用流程

```text
写入：Admin 提交明文 Key（HTTPS）
      → 内存中 Fernet.encrypt → 存密文 + hint + version
      → 明文立即从内存清除；写审计日志（不含明文）
读取：Provider 客户端初始化时 → Fernet.decrypt → 仅存在于进程内存
      → 请求结束不落盘、不写日志、不进错误堆栈（日志脱敏见《19-日志设计》）
展示：任何 API/界面永不返回明文，只返回 api_key_hint
校验：POST /providers/{id}/test 用解密后的 Key 真实调用一次验证有效性
```

### 4.3 管理规则

| 操作 | 权限 | 审计 | 备注 |
|------|------|------|------|
| 录入/更新 Key | Admin | ✅（记录操作人、时间、hint、provider） | 更新即覆盖，旧密文不保留 |
| 查看 Key | 任何人 | — | **不可能**：无解密展示功能 |
| 导出 Key | 任何人 | — | **禁止**：备份中仅含密文 |
| 主密钥轮换 | 服务器操作 | ✅ | 批量重加密，逐条校验 |
| Key 失效告警 | 系统 | ✅ | Provider 返回 AUTH_ERROR 时标记 Key 状态并通知 Admin（需求第三十四节：Auth Error 不无限重试） |

---

## 5. 操作审计日志

### 5.1 记录范围

**所有写操作**与**敏感读操作**必须记录审计日志：

| 类别 | 示例事件 |
|------|---------|
| 认证 | login_success / login_failed / logout / token_refresh / token_replay_detected / account_locked |
| 用户管理 | user_created / role_changed / user_disabled / password_reset |
| Provider 管理 | provider_created / provider_deleted / config_updated / priority_locked / key_updated / provider_tested / test_all_triggered |
| 任务控制 | job_run_manual / job_paused / job_resumed / job_skipped |
| 模型/策略 | model_activated / strategy_created / validation_triggered |
| 备份恢复 | backup_created / backup_restored（含二次确认记录） |
| 个人数据（敏感读） | portfolio_exported / ai_conversation_deleted / plan_deleted |
| 权限拒绝 | permission_denied（记录被拒绝的访问尝试，含目标资源与来源 IP） |
| 系统配置 | rate_limit_changed / global_config_updated |

普通市场数据读取（行情、指标查询）**不**记审计（量大且非敏感），只记常规访问日志（《19-日志设计》）。

### 5.2 审计表结构

```sql
CREATE TABLE audit_logs (
    id           BIGSERIAL PRIMARY KEY,
    event_time   TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_id     UUID,                 -- 操作人（系统操作为 NULL）
    actor_role   VARCHAR(16),
    actor_name   VARCHAR(64),
    action       VARCHAR(64) NOT NULL, -- 如 provider.config_updated
    resource_type VARCHAR(32),         -- provider / user / job / plan ...
    resource_id  VARCHAR(64),
    result       VARCHAR(16) NOT NULL, -- SUCCESS / DENIED / FAILED
    detail       JSONB,                -- 变更 diff（新旧值，敏感字段脱敏后）
    ip           INET,
    user_agent   TEXT,
    request_id   VARCHAR(64),          -- 关联应用日志
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- TimescaleDB hypertable，按 event_time 分区；保留 ≥ 3 年
```

### 5.3 审计规则

- 审计写入与业务操作**同事务**（业务回滚则审计标记 FAILED 但保留记录）；写审计失败时业务操作告警但不静默吞掉；
- `detail` 中的变更 diff 必须脱敏：Key、密码字段只记 `"<masked>"` 与 hint；
- 审计日志**只增不改不删**：数据库层面回收 UPDATE/DELETE 权限（应用账号无删除权）；
- Admin 页面提供审计查询（按用户/动作/资源/时间过滤，敏感操作高亮）；
- 高危操作（恢复备份、删除 Provider、修改角色、激活模型）要求**二次确认**：前端确认 + 后端校验确认令牌（`40304` 未确认），确认行为本身也入审计。

---

## 6. 资源级访问控制

属主隔离统一规则（防 IDOR）：

| 资源 | 隔离键 | 规则 |
|------|--------|------|
| 计划 / 交易记录 / 持仓 / 绩效 | `user_id` | CRUD 全部强制 `WHERE user_id = current_user.id`；查询他人 ID 返回 40403 |
| 回测 run | `owner_id` | User 仅本人；Analyst/Admin 可查全部（研究需要），删除仅本人与 Admin |
| AI 会话 | `user_id` | 仅本人 |
| 策略 / 模型版本 | 无属主 | Analyst/Admin 共享；激活仅 Admin |
| Provider / 任务 / 备份 | 系统资源 | 管理操作仅 Admin；健康查看 Analyst+ |

实现约定：属主过滤在 **Repository 层强制执行**（查询构造器自动注入 user 条件），而非依赖各 API handler 自觉判断，从结构上杜绝遗漏。

---

## 7. 安全防护

| 威胁 | 对策 |
|------|------|
| 暴力破解 | 登录失败 5 次锁定 15 分钟；登录/刷新端点 IP 级限流；审计告警 |
| Token 重放 | refresh 旋转 + 链式重放检测（重用即全链吊销）；access jti 黑名单 |
| Token 泄露 | access 短有效期 30min；前端不落盘；HTTPS 强制；HSTS |
| IDOR 越权 | 第 6 节 Repository 层属主过滤；他人资源统一 404 语义 |
| CSRF | SameSite=Strict Cookie + 状态变更请求校验 Origin |
| 密钥泄露 | Fernet 加密 + 主密钥分离 + 日志全链路脱敏 + 备份不含明文 |
| 权限提升 | 角色变更仅 Admin 且入审计；JWT 中 role 以数据库实时值复核（Admin/Analyst 操作时二次查库，防旧 token 携带已降级角色） |
| 内部接口暴露 | `/docs`、`/redoc`、`/metrics` 生产环境需 Admin 认证或仅内网访问 |
| 注入 | SQLAlchemy 参数化查询；禁止拼接 SQL |
| 拒绝服务 | 第 6 节 Rate Limiting（《17-API 设计》）+ 重操作并发上限 |

---

## 附：默认账户与初始化

- 系统首次启动通过 `scripts/init_admin.py` 创建唯一 Admin（用户名/密码由交互式输入或环境变量注入，绝不硬编码默认密码）；
- 默认注册开放角色为 `user`；`analyst` / `admin` 只能由 Admin 提升；
- 单机自用场景下，注册可通过全局配置开关关闭（`ALLOW_REGISTRATION=false`）。

---

## 相关文档

| 文档 | 关联说明 |
|------|----------|
| [17-api-design.md](17-api-design.md) | API 端点定义（权限矩阵对应各端点） |
| [09-core-tables.md](09-core-tables.md) | 数据库表结构（users 表、user_role ENUM、audit_logs 表） |
| [19-logging-design.md](19-logging-design.md) | 审计日志实现机制 |
| [21-deployment.md](21-deployment.md) | 部署环境安全配置（JWT Secret、HTTPS） |
