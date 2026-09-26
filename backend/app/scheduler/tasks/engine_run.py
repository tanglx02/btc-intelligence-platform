"""引擎运行任务：周期/估值/风险/市场状态引擎定时计算与持久化。

每个任务：构造服务 → 引擎实例（persist=False）→ calculate() → 显式 save_result()
（避免引擎默认 persist 双写），失败向上抛出由调度器记录。
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from app.services.derivatives_service import DerivativesService
from app.services.etf_service import ETFService
from app.services.macro_service import MacroService
from app.services.market_service import MarketService
from app.services.onchain_service import OnChainService
from app.services.provider_service import get_provider_service
from app.services.sentiment_service import SentimentService

_STARTUP_LOCK = asyncio.Lock()


async def _ensure_provider_service():
    """获取全局 ProviderService 并确保已启动（惰性）。"""
    ps = get_provider_service()
    if not getattr(ps, "_started", False):
        async with _STARTUP_LOCK:
            if not getattr(ps, "_started", False):
                await ps.startup()
    return ps


async def _build_services() -> dict[str, Any]:
    """构造六个类目业务服务（共享同一 ProviderManager）。"""
    ps = await _ensure_provider_service()
    manager = ps.manager
    return {
        "market": MarketService(manager),
        "onchain": OnChainService(manager),
        "etf": ETFService(manager),
        "derivatives": DerivativesService(manager),
        "macro": MacroService(manager),
        "sentiment": SentimentService(manager),
    }


def _output_payload(output: Any, saved: bool) -> dict[str, Any]:
    """EngineOutput → 任务返回摘要。"""
    return {
        "state": output.state,
        "confidence": round(float(output.confidence), 4) if output.confidence is not None else None,
        "explanation": output.explanation,
        "observation_time": (
            output.observation_time.isoformat() if output.observation_time else None
        ),
        "saved": saved,
    }


async def run_valuation_engine() -> dict[str, Any]:
    """估值引擎（MVRV/NUPL 等维度综合）计算并持久化（任务 engine_valuation_4h）。"""
    from app.engines.valuation.engine import ValuationEngine

    services = await _build_services()
    engine = ValuationEngine(
        onchain_service=services["onchain"],
        market_service=services["market"],
        persist=False,
    )
    output = await engine.calculate()
    saved = await engine.save_result(output)
    logger.info("估值引擎完成 state={} saved={}", output.state, saved)
    return _output_payload(output, saved)


async def run_cycle_engine() -> dict[str, Any]:
    """周期引擎（四阶段周期定位）计算并持久化（任务 engine_cycle_4h）。"""
    from app.engines.cycle.engine import CycleEngine
    from app.engines.valuation.engine import ValuationEngine

    services = await _build_services()
    valuation = ValuationEngine(
        onchain_service=services["onchain"],
        market_service=services["market"],
        persist=False,
    )
    engine = CycleEngine(
        market_service=services["market"],
        onchain_service=services["onchain"],
        etf_service=services["etf"],
        derivatives_service=services["derivatives"],
        macro_service=services["macro"],
        sentiment_service=services["sentiment"],
        valuation_engine=valuation,
        persist=False,
    )
    output = await engine.calculate()
    saved = await engine.save_result(output)
    logger.info("周期引擎完成 state={} saved={}", output.state, saved)
    return _output_payload(output, saved)


async def run_risk_engine() -> dict[str, Any]:
    """风险引擎（多维风险评分）计算并持久化（任务 engine_risk_4h）。"""
    from app.engines.risk.engine import RiskEngine
    from app.engines.valuation.engine import ValuationEngine

    services = await _build_services()
    valuation = ValuationEngine(
        onchain_service=services["onchain"],
        market_service=services["market"],
        persist=False,
    )
    engine = RiskEngine(
        market_service=services["market"],
        derivatives_service=services["derivatives"],
        onchain_service=services["onchain"],
        macro_service=services["macro"],
        sentiment_service=services["sentiment"],
        valuation_engine=valuation,
        persist=False,
    )
    output = await engine.calculate()
    saved = await engine.save_result(output)
    logger.info("风险引擎完成 state={} saved={}", output.state, saved)
    return _output_payload(output, saved)


async def run_regime_engine() -> dict[str, Any]:
    """市场状态引擎（融合三引擎输出）计算并持久化（任务 engine_regime_12h）。

    注意：regime 引擎 calculate() 内部自跑周期/估值/风险三个子引擎，
    此处子引擎均 persist=False，仅由本任务对 regime 输出显式 save_result。
    """
    from app.engines.cycle.engine import CycleEngine
    from app.engines.regime.engine import MarketRegimeEngine
    from app.engines.risk.engine import RiskEngine
    from app.engines.valuation.engine import ValuationEngine

    services = await _build_services()
    valuation = ValuationEngine(
        onchain_service=services["onchain"],
        market_service=services["market"],
        persist=False,
    )
    cycle = CycleEngine(
        market_service=services["market"],
        onchain_service=services["onchain"],
        etf_service=services["etf"],
        derivatives_service=services["derivatives"],
        macro_service=services["macro"],
        sentiment_service=services["sentiment"],
        valuation_engine=valuation,
        persist=False,
    )
    risk = RiskEngine(
        market_service=services["market"],
        derivatives_service=services["derivatives"],
        onchain_service=services["onchain"],
        macro_service=services["macro"],
        sentiment_service=services["sentiment"],
        valuation_engine=valuation,
        persist=False,
    )
    engine = MarketRegimeEngine(
        market_service=services["market"],
        onchain_service=services["onchain"],
        etf_service=services["etf"],
        derivatives_service=services["derivatives"],
        macro_service=services["macro"],
        sentiment_service=services["sentiment"],
        cycle_engine=cycle,
        valuation_engine=valuation,
        risk_engine=risk,
        persist=False,
    )
    output = await engine.calculate()
    saved = await engine.save_result(output)
    logger.info("市场状态引擎完成 state={} saved={}", output.state, saved)
    return _output_payload(output, saved)


__all__ = [
    "run_cycle_engine",
    "run_regime_engine",
    "run_risk_engine",
    "run_valuation_engine",
]
