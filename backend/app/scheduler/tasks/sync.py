"""增量同步任务：candles / onchain / etf / derivatives / macro 增量拉取。

SyncService 从 checkpoint 位置继续拉取到当前时刻，重叠回溯 + 幂等写入。
单个目标失败仅记录并继续；全部失败才向上抛出（由调度器记录 FAILED）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from app.services.provider_service import get_provider_service
from app.services.sync_service import SyncService

_STARTUP_LOCK = asyncio.Lock()

# (data_type, symbol, sync_incremental 额外 kwargs)
_SYNC_TARGETS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("candles", "BTCUSDT", {}),
    ("onchain", "BTC", {"metric": "mvrv"}),
    ("etf", "BTC", {}),
    ("derivatives", "BTCUSDT", {}),
    ("macro", "BTC", {"metric": "cpi"}),
)


async def _ensure_provider_service():
    """获取全局 ProviderService 并确保已启动（惰性）。"""
    ps = get_provider_service()
    if not getattr(ps, "_started", False):
        async with _STARTUP_LOCK:
            if not getattr(ps, "_started", False):
                await ps.startup()
    return ps


def _status_value(status: Any) -> str:
    """SyncStatus 枚举 → 字符串。"""
    return status.value if hasattr(status, "value") else str(status)


async def sync_incremental_data() -> dict[str, Any]:
    """按目标清单增量同步新数据（任务 incremental_sync_1h）。"""
    ps = await _ensure_provider_service()
    service = SyncService(provider_service=ps)

    outcomes: dict[str, dict[str, Any]] = {}
    failures = 0
    for data_type, symbol, extra in _SYNC_TARGETS:
        try:
            outcome = await service.sync_incremental(data_type, symbol, **extra)
            outcomes[data_type] = {
                "status": _status_value(outcome.status),
                "records_synced": outcome.records_synced,
                "inserted": outcome.inserted,
                "updated": outcome.updated,
                "error": outcome.error,
            }
            logger.info(
                "增量同步完成 data_type={} status={} inserted={} updated={}",
                data_type, _status_value(outcome.status), outcome.inserted, outcome.updated,
            )
        except Exception as exc:  # noqa: BLE001
            failures += 1
            outcomes[data_type] = {"status": "FAILED", "error": str(exc)}
            logger.warning("增量同步失败 data_type={} symbol={}: {}", data_type, symbol, exc)

    if failures >= len(_SYNC_TARGETS):
        raise RuntimeError(f"增量同步全部失败（{failures}/{len(_SYNC_TARGETS)}）")

    return {
        "targets": len(_SYNC_TARGETS),
        "failures": failures,
        "outcomes": outcomes,
    }


__all__ = ["sync_incremental_data"]
