"""默认调度任务注册表。

register_default_jobs(scheduler) 将全部内置任务注册到调度器，
由 get_scheduler() 惰性调用。任务函数来自 app.scheduler.tasks。
"""

from __future__ import annotations

from app.scheduler.scheduler import SchedulerService, TaskDefinition
from app.scheduler.tasks import (
    calculate_indicators,
    collect_candles,
    collect_market_data,
    generate_daily_snapshot,
    run_cycle_engine,
    run_gap_detection,
    run_quality_check,
    run_regime_engine,
    run_risk_engine,
    run_valuation_engine,
    sync_incremental_data,
)

_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600


def register_default_jobs(scheduler: SchedulerService) -> None:
    """注册所有默认任务。

    优先级：数据采集(10-19) < 同步(20-29) < 指标(30-39) < 引擎(40-49)
    < 质量(60-69) < 快照(70-79)；数值越小越先启动。
    initial_delay 依次错开，避免启动时任务集中执行。
    """
    scheduler.register(
        TaskDefinition(
            name="market_1m",
            coroutine=collect_market_data,
            interval_seconds=_SECONDS_PER_MINUTE,
            priority=10,
            description="每分钟采集BTC价格数据",
            job_group="MARKET",
            initial_delay=0,
        )
    )
    scheduler.register(
        TaskDefinition(
            name="market_candles_15m",
            coroutine=collect_candles,
            interval_seconds=15 * _SECONDS_PER_MINUTE,
            priority=15,
            description="每15分钟采集BTC日线K线（回看3天）",
            job_group="MARKET",
            initial_delay=10,
        )
    )
    scheduler.register(
        TaskDefinition(
            name="incremental_sync_1h",
            coroutine=sync_incremental_data,
            interval_seconds=_SECONDS_PER_HOUR,
            priority=20,
            description="每小时增量同步 candles/onchain/etf/derivatives/macro 数据",
            job_group="SYSTEM",
            initial_delay=15,
        )
    )
    scheduler.register(
        TaskDefinition(
            name="indicator_calc_1h",
            coroutine=calculate_indicators,
            interval_seconds=_SECONDS_PER_HOUR,
            priority=30,
            description="每小时基于本地日线数据批量计算核心指标",
            job_group="SYSTEM",
            initial_delay=20,
        )
    )
    scheduler.register(
        TaskDefinition(
            name="engine_cycle_4h",
            coroutine=run_cycle_engine,
            interval_seconds=4 * _SECONDS_PER_HOUR,
            priority=40,
            description="每4小时运行周期引擎",
            job_group="ENGINE",
            initial_delay=25,
        )
    )
    scheduler.register(
        TaskDefinition(
            name="engine_valuation_4h",
            coroutine=run_valuation_engine,
            interval_seconds=4 * _SECONDS_PER_HOUR,
            priority=41,
            description="每4小时运行估值引擎",
            job_group="ENGINE",
            initial_delay=30,
        )
    )
    scheduler.register(
        TaskDefinition(
            name="engine_risk_4h",
            coroutine=run_risk_engine,
            interval_seconds=4 * _SECONDS_PER_HOUR,
            priority=42,
            description="每4小时运行风险引擎",
            job_group="ENGINE",
            initial_delay=35,
        )
    )
    scheduler.register(
        TaskDefinition(
            name="engine_regime_12h",
            coroutine=run_regime_engine,
            interval_seconds=12 * _SECONDS_PER_HOUR,
            priority=43,
            description="每12小时运行市场状态引擎（融合三引擎）",
            job_group="ENGINE",
            initial_delay=40,
        )
    )
    scheduler.register(
        TaskDefinition(
            name="quality_check_6h",
            coroutine=run_quality_check,
            interval_seconds=6 * _SECONDS_PER_HOUR,
            priority=60,
            description="每6小时执行价格一致性数据质量检查",
            job_group="SYSTEM",
            initial_delay=45,
        )
    )
    scheduler.register(
        TaskDefinition(
            name="gap_detection_1d",
            coroutine=run_gap_detection,
            interval_seconds=24 * _SECONDS_PER_HOUR,
            priority=61,
            description="每天检测并补齐日线K线缺口（回看7天）",
            job_group="SYSTEM",
            initial_delay=50,
        )
    )
    scheduler.register(
        TaskDefinition(
            name="daily_snapshot_1d",
            coroutine=generate_daily_snapshot,
            interval_seconds=24 * _SECONDS_PER_HOUR,
            priority=70,
            description="每天为所有用户生成组合净值快照",
            job_group="SYSTEM",
            initial_delay=55,
        )
    )


__all__ = ["register_default_jobs"]
