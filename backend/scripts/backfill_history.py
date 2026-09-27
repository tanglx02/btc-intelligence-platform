"""一次性历史回填脚本：candles 1d / 1h 从 2017-08-17（Binance 上线）至今。

复用 SyncService 完整链路（断点续传 checkpoint + 令牌桶限速 + 幂等 upsert +
Redis 分布式锁），数据经 Provider 层获取（binance 走 SOCKS5 代理，配置在 DB）。

用法（在 backend/ 目录下）::

    .\\.venv\\Scripts\\python.exe scripts\\backfill_history.py             # 1d + 1h 全量
    .\\.venv\\Scripts\\python.exe scripts\\backfill_history.py --only 1d   # 只回填日线
    .\\.venv\\Scripts\\python.exe scripts\\backfill_history.py --only 1h   # 只回填小时线

说明：
- 重复执行安全：已有区间由 checkpoint / 幂等写入去重，只会补缺口；
- Provider 配置优先读 DB（config_overrides / proxy_config），YAML 兜底；
- backend/.env 的 PROXY_SOCKS5_URL 会被注入进程环境变量（供 YAML ${VAR} 解析）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# 保证以 backend/ 为工作目录（providers.yaml 相对路径依赖）
BACKEND_DIR = Path(__file__).resolve().parent.parent
os.chdir(BACKEND_DIR)
sys.path.insert(0, str(BACKEND_DIR))


def _load_dotenv() -> None:
    """轻量 .env 加载：仅注入 os.environ 中尚不存在的键（不覆盖已有）。"""
    env_path = BACKEND_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


# Binance BTCUSDT 上线时间（首个 1d K 线）
BINANCE_BTCUSDT_START = datetime(2017, 8, 17, tzinfo=timezone.utc)


async def backfill(interval: str) -> dict:
    """回填指定周期的 candles 历史。"""
    from app.services.provider_service import get_provider_service
    from app.services.sync_service import SyncService

    ps = get_provider_service()
    if not getattr(ps, "_started", False):
        await ps.startup()
    sync = SyncService(provider_service=ps)

    end = datetime.now(timezone.utc)
    print(f"[backfill] candles interval={interval} {BINANCE_BTCUSDT_START:%Y-%m-%d} -> {end:%Y-%m-%d} ...")
    outcome = await sync.sync_historical(
        data_type="candles",
        symbol="BTCUSDT",
        start_date=BINANCE_BTCUSDT_START,
        end_date=end,
        provider="binance",
        interval=interval,
    )
    info = outcome.as_dict() if hasattr(outcome, "as_dict") else repr(outcome)
    print(f"[backfill] interval={interval} result: {info}")
    return info if isinstance(info, dict) else {"raw": str(info)}


async def summarize() -> None:
    """打印 candles 表现状。"""
    from sqlalchemy import text

    from app.core.database import get_db_session_ctx

    async with get_db_session_ctx() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT interval, symbol, count(*) AS n, min(observation_time) AS mn, max(observation_time) AS mx "
                    "FROM candles GROUP BY interval, symbol ORDER BY interval, symbol"
                )
            )
        ).mappings().all()
        if not rows:
            print("[summary] candles 表中无 BTCUSDT 数据")
            return
        for r in rows:
            print(
                f"[summary] interval={r['interval']:<4s} symbol={r['symbol']:<10s} count={r['n']:>7d} "
                f"range={r['mn']} -> {r['mx']}"
            )


async def main() -> None:
    parser = argparse.ArgumentParser(description="candles 历史回填（binance, SOCKS5 代理）")
    parser.add_argument("--only", choices=["1d", "1h"], default=None, help="仅回填指定周期")
    args = parser.parse_args()

    _load_dotenv()
    intervals = [args.only] if args.only else ["1d", "1h"]

    results: dict[str, dict] = {}
    try:
        for iv in intervals:
            results[iv] = await backfill(iv)
    finally:
        # 打印数据现状（不 shutdown provider service——进程即将退出）
        try:
            await summarize()
        except Exception as exc:  # noqa: BLE001
            print(f"[summary] 统计失败: {exc}")

    failed = [iv for iv, r in results.items() if str(r.get("status", "")).upper() not in ("COMPLETED", "COMPLETED_WITH_ERRORS")]
    if failed:
        print(f"[backfill] 存在未完成周期: {failed}")
        sys.exit(1)
    print("[backfill] 全部完成")


if __name__ == "__main__":
    asyncio.run(main())
