# 数据库恢复说明（db_backup）

交接包内含平台运行至今的完整数据快照 `btc_platform.dump`（PostgreSQL -Fc 自定义格式），恢复后可直接延续开发，无需重新回填历史数据。dump 文件本身不纳入 git（见 .gitignore），仅随交接包分发。

## 备份内容（导出时间 2026-09-27）

- `candles` 83,083 行（BTC/USDT 1h + 1d K线，2017-08-17 → 2026-09-27）
- `market_prices` 201 行、`indicator_values` 指标结果
- `providers` 24 个数据源配置（含加密 API Key 字段、代理配置）
- 引擎输出（cycle_states / valuation_states / risk_scores / market_regimes）
- system_settings 配置中心、alert_rules / alert_events 预警数据
- 全部 ENUM 类型、索引、触发器、外键

## 恢复步骤

### 场景 A：本机已有 PostgreSQL（当前部署机）
PostgreSQL 16 便携版在 `D:\btc-local-deploy\pgsql`，服务名 `pgsql-btc`，库 `btc_platform`（trust 认证，用户 postgres）。数据在运行中，通常无需恢复。如需重置：

```powershell
# 先停后端 uvicorn（避免连接占用）
& "D:\btc-local-deploy\pgsql\bin\pg_restore.exe" -h 127.0.0.1 -p 5432 -U postgres -d btc_platform --clean --if-exists "交接包路径\db_backup\btc_platform.dump"
```

### 场景 B：全新机器
1. 安装 PostgreSQL 16（普通版即可，迁移不强依赖 TimescaleDB 扩展；装 TimescaleDB 镜像更佳）
2. 建库：`createdb -U postgres btc_platform`
3. 恢复：`pg_restore -U postgres -d btc_platform btc_platform.dump`（`--clean --if-exists` 可重复覆盖）
4. 配置 `.env`：POSTGRES_HOST/PORT/DB/USER/PASSWORD 指向新库
5. 启动后端，数据即刻可用

## 校验恢复成功
```sql
SELECT count(*) FROM candles;   -- 期望 83083
SELECT count(*) FROM providers; -- 期望 24
```

## 注意
- dump 用 pg_dump 16 生成，建议 pg_restore 16 恢复（大版本一致）
- providers 表 api_key_encrypted 用 Fernet 加密，密钥派生自 `.env` 的 SECRET_KEY / SETTINGS_ENCRYPTION_KEY——恢复后必须使用相同 `.env`（交接包已附），否则已存 API Key 无法解密（不影响无 Key 数据源）
- 恢复后重启后端调度器会自动增量采集；或 `python scripts/backfill_history.py` 重新回填
