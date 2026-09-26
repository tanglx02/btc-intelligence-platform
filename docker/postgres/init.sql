-- BTC 全市场智能研究平台 - 数据库初始化脚本
-- 由 docker-entrypoint-initdb.d 在首次启动时自动执行

-- 启用 TimescaleDB 扩展（时序数据 / 超表支持）
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- 启用 UUID 扩展（主键生成）
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 启用 pg_trgm 用于模糊搜索
CREATE EXTENSION IF NOT EXISTS pg_trgm;
