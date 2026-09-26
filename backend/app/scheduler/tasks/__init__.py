"""调度器任务函数集合。

约定：
- 全部为 async 函数；
- 自行处理依赖初始化（ProviderService 全局单例惰性启动等）；
- 异常向上抛出，由 SchedulerService 统一记录（单任务失败不影响其他任务）。
"""

from .data_quality import run_gap_detection, run_quality_check
from .engine_run import (
    run_cycle_engine,
    run_regime_engine,
    run_risk_engine,
    run_valuation_engine,
)
from .indicator_calc import calculate_indicators
from .market_collect import collect_candles, collect_market_data
from .snapshot import generate_daily_snapshot
from .sync import sync_incremental_data

__all__ = [
    "calculate_indicators",
    "collect_candles",
    "collect_market_data",
    "generate_daily_snapshot",
    "run_cycle_engine",
    "run_gap_detection",
    "run_quality_check",
    "run_regime_engine",
    "run_risk_engine",
    "run_valuation_engine",
    "sync_incremental_data",
]
