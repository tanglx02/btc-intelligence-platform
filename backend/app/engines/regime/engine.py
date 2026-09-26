"""MarketRegimeEngine — 综合市场状态引擎（架构文档 §4）。

唯一面向用户的「当前市场怎么样」总出口：
- 融合 9 个维度（趋势/估值/资金流/链上/衍生品/宏观/情绪/风险/周期）；
- 输出描述性 Regime 标签（Risk-On Expansion / Cautious Optimism / Neutral /
  Risk-Off Contraction / Capitulation / Euphoria），永不输出 BUY/SELL；
- 每日快照持久化到 market_regimes（无论是否变化），状态变化另写 signals；
- 支持查询任意历史日期系统当时的判断（get_at_date，as-run 语义）。

降级规则：任一维度不可用 → 跳过该维度、重归一化权重、置信度下降、
缺口显式声明（禁止用历史均值冒充当前值）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import select

from app.core.database import get_db_session_ctx
from app.engines.base import (
    EngineBase,
    EngineEvidence,
    EngineOutput,
    clamp,
    quality_coefficient,
    safe_div,
)
from app.engines.regime.fusion import (
    DimensionInput,
    FusionResult,
    RegimeFusion,
)
from app.models.engine import MarketRegime
from app.models.enums import (
    CyclePhase,
    QualityStatus,
    RiskLevel,
    SignalDirection,
    ValuationLevel,
)

__all__ = [
    "MarketRegimeEngine",
    "get_regime_engine",
    "set_regime_engine",
]


def _utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _extract(data: Any, *keys: str) -> float | None:
    """从 ServiceResult.data 提取第一个可用数值。"""
    if data is None:
        return None
    if isinstance(data, (int, float)):
        return float(data) if data == data else None
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if v is not None:
                try:
                    f = float(v)
                    if f == f:
                        return f
                except (TypeError, ValueError):
                    continue
    return None


def _qstr(result: Any) -> str:
    qs = getattr(result, "quality_status", None)
    return qs.value if hasattr(qs, "value") else str(qs or "VERIFIED")


class MarketRegimeEngine(EngineBase):
    """综合市场状态引擎。

    Usage::

        engine = MarketRegimeEngine(
            market_service=..., onchain_service=..., etf_service=...,
            derivatives_service=..., macro_service=..., sentiment_service=...,
            cycle_engine=..., valuation_engine=..., risk_engine=...,
        )
        output = await engine.calculate()      # state=Regime 标签（英文）
        latest = await engine.get_latest()     # 最新快照
        past = await engine.get_at_date(date)  # 任意历史日期的系统判断
    """

    def __init__(
        self,
        *,
        market_service: Any = None,
        onchain_service: Any = None,
        etf_service: Any = None,
        derivatives_service: Any = None,
        macro_service: Any = None,
        sentiment_service: Any = None,
        cycle_engine: Any = None,
        valuation_engine: Any = None,
        risk_engine: Any = None,
        fusion: RegimeFusion | None = None,
        dimension_weights: dict[str, float] | None = None,
        persist: bool = True,
    ) -> None:
        self._market = market_service
        self._onchain = onchain_service
        self._etf = etf_service
        self._derivatives = derivatives_service
        self._macro = macro_service
        self._sentiment = sentiment_service
        self._cycle_engine = cycle_engine
        self._valuation_engine = valuation_engine
        self._risk_engine = risk_engine
        self._fusion = fusion or RegimeFusion(
            weights=dimension_weights if dimension_weights else None
        )
        self._persist = persist

    @property
    def name(self) -> str:
        return "regime"

    @property
    def _orm_model(self) -> type:
        return MarketRegime

    # ---- 主计算 ----

    async def calculate(self, as_of: datetime | None = None) -> EngineOutput:
        """计算当前（或指定时刻）的综合市场状态。"""
        obs = _utc(as_of)
        logger.info(f"[regime] 开始计算综合市场状态 as_of={obs.isoformat()}")

        # 1. 三个子引擎输出（并发，各自内部会持久化）
        cycle_out, valuation_out, risk_out = await self._run_sub_engines(obs)

        # 2. 六个数据维度（并发）
        data_dims = await self._compute_data_dimensions(obs)

        # 3. 组装 9 维输入
        dimensions: list[DimensionInput] = list(data_dims.values())
        dimensions.append(self._engine_dimension(
            "valuation", valuation_out, "估值引擎不可用", ValuationLevel.FAIR.value,
        ))
        dimensions.append(self._engine_dimension(
            "risk", risk_out, "风险引擎不可用", RiskLevel.MODERATE.value,
        ))
        dimensions.append(self._engine_dimension(
            "cycle", cycle_out, "周期引擎不可用", CyclePhase.UPTREND.value,
        ))

        # 4. 融合 + 定性修正
        result: FusionResult = self._fusion.fuse(
            dimensions,
            risk_level=risk_out.state if risk_out else None,
            cycle_phase=cycle_out.state if cycle_out else None,
            valuation_level=valuation_out.state if valuation_out else None,
        )

        # 5. 变化追踪（与上一快照比较）
        prev_regime, is_changed, change_reason = await self._detect_change(result, obs)

        # 6. 构建输出
        evidence: list[EngineEvidence] = []
        for d in dimensions:
            evidence.extend(d.evidence)
        output = self._build_output(
            state=result.label,
            score=result.regime_score,
            evidence=evidence or [self._neutral_evidence(result)],
            explanation=result.explanation,
            observation_time=obs,
            data_quality=self._overall_quality(evidence),
            dimension_scores=result.dimension_scalars,
            data_gaps=result.data_gaps,
            metadata={
                "label_cn": result.label_cn,
                "dimension_states": result.dimension_states,
                "consistency": result.consistency,
                "divergence_flag": result.divergence_flag,
                "corrections": result.corrections,
                "previous_regime": prev_regime,
                "is_changed": is_changed,
                "change_reason": change_reason,
                "sub_engine_outputs": {
                    "cycle": cycle_out.state if cycle_out else None,
                    "valuation": valuation_out.state if valuation_out else None,
                    "risk": risk_out.state if risk_out else None,
                },
            },
            base_confidence=0.9,
        )
        output.confidence = result.confidence  # 融合器已计算（一致性×覆盖率×质量）
        logger.info(
            f"[regime] 计算完成: label={result.label} score={result.regime_score:+.3f} "
            f"confidence={output.confidence:.2f} changed={is_changed}"
        )

        # 7. 持久化（每日快照，无论是否变化）+ 变化信号
        if self._persist:
            await self.save_result(output)
            if is_changed and prev_regime:
                await self._emit_change_signal(output, prev_regime, change_reason)
        return output

    async def calculate_historical(
        self, start: datetime, end: datetime
    ) -> list[EngineOutput]:
        """按日计算历史 Regime 序列（逐日推进，保证变化追踪时序正确）。"""
        outputs: list[EngineOutput] = []
        day = _utc(start)
        end_utc = _utc(end)
        while day <= end_utc:
            try:
                out = await self.calculate(as_of=day)
                outputs.append(out)
            except Exception as exc:
                logger.error(f"[regime] 历史计算失败 {day.date()}: {exc!r}")
            day += timedelta(days=1)
        logger.info(f"[regime] 历史计算完成: {len(outputs)} 天")
        return outputs

    # ---- 子引擎 ----

    async def _run_sub_engines(
        self, obs: datetime
    ) -> tuple[EngineOutput | None, EngineOutput | None, EngineOutput | None]:
        """并发运行 Cycle/Valuation/Risk 引擎（失败降级为 None）。"""
        async def _safe(engine: Any, label: str) -> EngineOutput | None:
            if engine is None:
                return None
            try:
                return await engine.calculate(as_of=obs)
            except Exception as exc:
                logger.warning(f"[regime] {label} 调用失败（该维度降级）: {exc!r}")
                return None

        cycle_out, valuation_out, risk_out = await asyncio.gather(
            _safe(self._cycle_engine, "周期引擎"),
            _safe(self._valuation_engine, "估值引擎"),
            _safe(self._risk_engine, "风险引擎"),
        )
        return cycle_out, valuation_out, risk_out

    def _engine_dimension(
        self, key: str, out: EngineOutput | None, unavailable_reason: str, default_state: str
    ) -> DimensionInput:
        """子引擎输出 → DimensionInput。"""
        from app.engines.regime.fusion import RegimeFusion as _F

        if out is None:
            return DimensionInput(
                key=key, value=None, state=default_state,
                quality_status="INVALID", confidence=0.0,
                evidence=[EngineEvidence(
                    factor=f"{key} 维度", value=None,
                    interpretation=f"{unavailable_reason}，该维度不参与本次融合",
                    weight=0.0, supports="neutral", confidence=0.0,
                    data_source="regime_engine", quality_status="INVALID",
                )],
            )
        scalar = _F.state_to_scalar(key, out.state)
        return DimensionInput(
            key=key, value=scalar, state=out.state,
            quality_status=out.data_quality, confidence=out.confidence,
            evidence=list(out.supporting_evidence[:3]) + list(out.opposing_evidence[:2]),
        )

    # ---- 六个数据维度 ----

    async def _compute_data_dimensions(
        self, obs: datetime
    ) -> dict[str, DimensionInput]:
        """并发计算趋势/资金流/链上/衍生品/宏观/情绪 6 个维度。"""
        results = await asyncio.gather(
            self._safe_dim("trend", self._dim_trend(obs)),
            self._safe_dim("capital_flow", self._dim_capital_flow(obs)),
            self._safe_dim("onchain", self._dim_onchain(obs)),
            self._safe_dim("derivative", self._dim_derivative(obs)),
            self._safe_dim("macro", self._dim_macro(obs)),
            self._safe_dim("sentiment", self._dim_sentiment(obs)),
        )
        return {d.key: d for d in results}

    @staticmethod
    async def _safe_dim(key: str, coro: Any) -> DimensionInput:
        try:
            return await coro
        except Exception as exc:
            logger.warning(f"[regime] 维度 {key} 计算异常: {exc!r}")
            return MarketRegimeEngine._gap_dimension(key, str(exc))

    @staticmethod
    def _gap_dimension(key: str, reason: str) -> DimensionInput:
        return DimensionInput(
            key=key, value=None, state="NEUTRAL",
            quality_status="INVALID", confidence=0.0,
            evidence=[EngineEvidence(
                factor=f"{key} 维度", value=None,
                interpretation=f"{key} 维度数据暂不可用（{reason}），不参与本次融合",
                weight=0.0, supports="neutral", confidence=0.0,
                data_source="regime_engine", quality_status="INVALID",
            )],
        )

    async def _dim_trend(self, obs: datetime) -> DimensionInput:
        """趋势维度：MA200 位置 + 30 日动量 → STRONG_UP/UP/NEUTRAL/DOWN/STRONG_DOWN。"""
        if self._market is None:
            return self._gap_dimension("trend", "行情服务未配置")
        res = await self._market.get_ohlcv(
            symbol="BTCUSDT", interval="1d",
            start=obs - timedelta(days=300), end=obs, limit=300,
        )
        if not res.success or not isinstance(res.data, list) or len(res.data) < 200:
            return self._gap_dimension("trend", res.error or "K 线数据不足")
        closes = [
            float(c["close"]) for c in res.data
            if c.get("close") is not None
        ]
        if len(closes) < 200:
            return self._gap_dimension("trend", "有效收盘价不足")

        ma200 = sum(closes[-200:]) / 200
        price = closes[-1]
        deviation = safe_div(price - ma200, ma200, 0.0)
        momentum_30d = safe_div(closes[-1] - closes[-31], closes[-31], 0.0) if len(closes) >= 31 else 0.0

        raw = clamp(deviation * 1.5 + momentum_30d * 1.2, -1.0, 1.0)
        if raw >= 0.5:
            state = "STRONG_UP"
            interp = f"价格高于 200 日均线 {deviation:+.0%} 且 30 日动量 {momentum_30d:+.0%}，趋势强劲向上"
        elif raw >= 0.15:
            state = "UP"
            interp = f"价格位于 200 日均线上方（{deviation:+.0%}），趋势向上但动能一般"
        elif raw <= -0.5:
            state = "STRONG_DOWN"
            interp = f"价格低于 200 日均线 {deviation:+.0%} 且 30 日动量 {momentum_30d:+.0%}，趋势强劲向下"
        elif raw <= -0.15:
            state = "DOWN"
            interp = f"价格位于 200 日均线下方（{deviation:+.0%}），趋势偏弱"
        else:
            state = "NEUTRAL"
            interp = f"价格在 200 日均线附近震荡（{deviation:+.0%}），趋势方向不明"

        from app.engines.regime.fusion import RegimeFusion as _F

        return DimensionInput(
            key="trend",
            value=_F.state_to_scalar("trend", state),
            state=state,
            quality_status=_qstr(res),
            confidence=min(1.0, len(closes) / 300),
            evidence=[EngineEvidence(
                factor="趋势状态", value={
                    "price": round(price, 2), "ma200": round(ma200, 2),
                    "deviation": round(deviation, 4),
                    "momentum_30d": round(momentum_30d, 4),
                },
                interpretation=interp,
                weight=self._fusion.weights.get("trend", 0.15),
                supports="bullish" if raw > 0.1 else ("bearish" if raw < -0.1 else "neutral"),
                confidence=min(1.0, len(closes) / 300) * quality_coefficient(_qstr(res)),
                data_source="market_service/ohlcv",
                indicator_code="tech.sma",
                quality_status=_qstr(res),
            )],
        )

    async def _dim_capital_flow(self, obs: datetime) -> DimensionInput:
        """资金流维度：交易所 7 日净流入 + ETF 7 日净流 → INFLOW/OUTFLOW 状态。"""
        if self._onchain is None and self._etf is None:
            return self._gap_dimension("capital_flow", "资金流服务未配置")

        net_btc: float | None = None
        etf_usd: float | None = None
        qualities: list[str] = []

        if self._onchain is not None:
            try:
                flows = await self._onchain.get_exchange_flows(obs - timedelta(days=7), obs)
                if flows.success and isinstance(flows.data, list):
                    total = sum(
                        _extract(r, "net_flow_btc", "netflow_btc", "net_btc") or 0.0
                        for r in flows.data
                    )
                    net_btc = total
                    qualities.append(_qstr(flows))
            except Exception as exc:
                logger.debug(f"[regime] 交易所资金流失败: {exc!r}")

        if self._etf is not None:
            try:
                etf = await self._etf.get_net_flow(period="7d", end_date=obs)
                if etf.success:
                    etf_usd = _extract(etf.data, "net_flow_usd", "total_net_flow_usd")
                    qualities.append(_qstr(etf))
            except Exception as exc:
                logger.debug(f"[regime] ETF 资金流失败: {exc!r}")

        if net_btc is None and etf_usd is None:
            return self._gap_dimension("capital_flow", "交易所与 ETF 资金流均不可用")

        # 交易所流出（负值）= 看涨；ETF 净流入 = 看涨
        parts: list[float] = []
        weights: list[float] = []
        if net_btc is not None:
            parts.append(clamp(-net_btc / 5000.0, -1.0, 1.0))
            weights.append(0.5)
        if etf_usd is not None:
            parts.append(clamp(etf_usd / 1e9, -1.0, 1.0))
            weights.append(0.5)
        raw = sum(p * w for p, w in zip(parts, weights)) / (sum(weights) or 1.0)

        if raw >= 0.5:
            state, interp = "STRONG_INFLOW", "资金强劲流入（交易所净流出/ETF 大额申购），增量资金明显"
        elif raw >= 0.15:
            state, interp = "INFLOW", "资金温和流入，市场有增量资金支撑"
        elif raw <= -0.5:
            state, interp = "STRONG_OUTFLOW", "资金大幅流出（交易所大量流入/ETF 赎回），抛压明显"
        elif raw <= -0.15:
            state, interp = "OUTFLOW", "资金温和流出，增量资金不足"
        else:
            state, interp = "NEUTRAL", "资金进出基本平衡"

        from app.engines.regime.fusion import RegimeFusion as _F

        conf = (0.8 if len(parts) == 2 else 0.55) * (
            sum(quality_coefficient(q) for q in qualities) / len(qualities) if qualities else 0.5
        )
        return DimensionInput(
            key="capital_flow",
            value=_F.state_to_scalar("capital_flow", state),
            state=state,
            quality_status=qualities[0] if qualities else "ESTIMATED",
            confidence=conf,
            evidence=[EngineEvidence(
                factor="资金流状态",
                value={"exchange_netflow_7d_btc": net_btc, "etf_netflow_7d_usd": etf_usd},
                interpretation=interp,
                weight=self._fusion.weights.get("capital_flow", 0.15),
                supports="bullish" if raw > 0.1 else ("bearish" if raw < -0.1 else "neutral"),
                confidence=conf,
                data_source="onchain+etf",
                indicator_code="onchain.exchange_netflow",
                quality_status=qualities[0] if qualities else None,
            )],
        )

    async def _dim_onchain(self, obs: datetime) -> DimensionInput:
        """链上维度：SOPR + NUPL + LTH 供应变化 → 吸筹/派发状态。"""
        if self._onchain is None:
            return self._gap_dimension("onchain", "链上服务未配置")

        parts: list[float] = []
        weights: list[float] = []
        details: dict[str, Any] = {}
        qualities: list[str] = []

        # SOPR：>1 获利了结偏多（健康牛市），<1 割肉偏空
        try:
            r = await self._onchain.get_sopr(obs)
            if r.success:
                v = _extract(r.data, "value", "sopr")
                if v is not None:
                    details["sopr"] = v
                    qualities.append(_qstr(r))
                    parts.append(clamp((v - 1.0) * 8.0, -1.0, 1.0))
                    weights.append(0.35)
        except Exception as exc:
            logger.debug(f"[regime] SOPR 失败: {exc!r}")

        # NUPL：<0 全网亏损（底部特征 → 环境偏空的极端，但周期上偏底部）
        try:
            r = await self._onchain.get_nupl(obs)
            if r.success:
                v = _extract(r.data, "value", "nupl")
                if v is not None:
                    details["nupl"] = v
                    qualities.append(_qstr(r))
                    # NUPL 高 = 获利盘厚 = 派发压力；低 = 亏损 = 悲观环境
                    parts.append(clamp((v - 0.4) * 1.5, -1.0, 1.0) * 0.5)
                    weights.append(0.30)
        except Exception as exc:
            logger.debug(f"[regime] NUPL 失败: {exc!r}")

        # LTH 供应 90 日变化：增持 = 吸筹（+），减持 = 派发（−）
        try:
            r_now = await self._onchain.get_lth_supply(obs)
            r_prev = await self._onchain.get_lth_supply(obs - timedelta(days=90))
            v_now = _extract(r_now.data, "value") if r_now.success else None
            v_prev = _extract(r_prev.data, "value") if r_prev.success else None
            if v_now is not None and v_prev is not None and v_prev > 0:
                change = safe_div(v_now - v_prev, v_prev, 0.0)
                details["lth_supply_change_90d"] = change
                qualities.append(_qstr(r_now))
                parts.append(clamp(change / 0.05, -1.0, 1.0))  # ±5% 映射到 ±1
                weights.append(0.35)
        except Exception as exc:
            logger.debug(f"[regime] LTH 供应失败: {exc!r}")

        if not parts:
            return self._gap_dimension("onchain", "SOPR/NUPL/LTH 数据均不可用")

        raw = sum(p * w for p, w in zip(parts, weights)) / (sum(weights) or 1.0)
        if raw >= 0.4:
            state, interp = "ACCUMULATION", "链上行为呈现吸筹特征（长期持有者增持、获利了结健康）"
        elif raw >= 0.12:
            state, interp = "NEUTRAL_ACC", "链上行为略偏吸筹"
        elif raw <= -0.4:
            state, interp = "DISTRIBUTION", "链上行为呈现派发特征（长期持有者减持、割肉或大规模获利了结）"
        elif raw <= -0.12:
            state, interp = "NEUTRAL_DIST", "链上行为略偏派发"
        else:
            state, interp = "NEUTRAL", "链上吸筹与派发力量均衡"

        from app.engines.regime.fusion import RegimeFusion as _F

        conf = min(1.0, len(parts) / 3 + 0.2) * (
            sum(quality_coefficient(q) for q in qualities) / len(qualities)
        )
        return DimensionInput(
            key="onchain",
            value=_F.state_to_scalar("onchain", state),
            state=state,
            quality_status=qualities[0] if qualities else "ESTIMATED",
            confidence=conf,
            evidence=[EngineEvidence(
                factor="链上状态",
                value=details,
                interpretation=interp,
                weight=self._fusion.weights.get("onchain", 0.15),
                supports="bullish" if raw > 0.1 else ("bearish" if raw < -0.1 else "neutral"),
                confidence=conf,
                data_source="onchain_service",
                indicator_code="onchain.sopr",
                quality_status=qualities[0] if qualities else None,
            )],
        )

    async def _dim_derivative(self, obs: datetime) -> DimensionInput:
        """衍生品维度：Funding + 清算 → CALM/MILD_BULL/OVERHEATED/MILD_BEAR/STRESSED。"""
        if self._derivatives is None:
            return self._gap_dimension("derivative", "衍生品服务未配置")

        funding_annualized: float | None = None
        liquidation_usd: float | None = None
        qualities: list[str] = []
        try:
            r = await self._derivatives.get_funding_rate("BTC/USDT")
            if r.success:
                fr = _extract(r.data, "funding_rate")
                if fr is not None:
                    funding_annualized = fr * 3 * 365
                    qualities.append(_qstr(r))
        except Exception as exc:
            logger.debug(f"[regime] Funding 失败: {exc!r}")
        try:
            r = await self._derivatives.get_liquidation("BTC/USDT")
            if r.success:
                liquidation_usd = _extract(r.data, "liquidation_total_usd", "total_usd")
                if liquidation_usd is not None:
                    qualities.append(_qstr(r))
        except Exception as exc:
            logger.debug(f"[regime] 清算数据失败: {exc!r}")

        if funding_annualized is None and liquidation_usd is None:
            return self._gap_dimension("derivative", "Funding 与清算数据均不可用")

        a = abs(funding_annualized) if funding_annualized is not None else 0.0
        heavy_liq = (liquidation_usd or 0) > 5e8

        if heavy_liq and a > 0.3:
            state = "STRESSED"
            interp = "衍生品市场承压：资金费率极端且清算量大，杠杆结构脆弱"
            value = -0.7
        elif a > 0.5:
            state = "OVERHEATED"
            interp = f"衍生品过热：资金费率年化 {funding_annualized:+.0%}，杠杆情绪亢奋"
            value = -0.2
        elif funding_annualized is not None and funding_annualized > 0.1:
            state = "MILD_BULL"
            interp = "衍生品温和偏多：多头愿意支付资金费，但杠杆未过热"
            value = 0.4
        elif funding_annualized is not None and funding_annualized < -0.1:
            state = "MILD_BEAR"
            interp = "衍生品温和偏空：空头占优支付资金费"
            value = -0.4
        else:
            state = "CALM"
            interp = "衍生品市场平静：资金费率处于常态区间"
            value = 0.0

        conf = (0.8 if (funding_annualized is not None and liquidation_usd is not None) else 0.6) * (
            sum(quality_coefficient(q) for q in qualities) / len(qualities) if qualities else 0.5
        )
        return DimensionInput(
            key="derivative",
            value=value,
            state=state,
            quality_status=qualities[0] if qualities else "ESTIMATED",
            confidence=conf,
            evidence=[EngineEvidence(
                factor="衍生品状态",
                value={
                    "funding_annualized": round(funding_annualized, 4) if funding_annualized is not None else None,
                    "liquidation_24h_usd": liquidation_usd,
                },
                interpretation=interp,
                weight=self._fusion.weights.get("derivative", 0.10),
                supports="bullish" if value > 0.1 else ("bearish" if value < -0.1 else "neutral"),
                confidence=conf,
                data_source="derivatives_service",
                indicator_code="deriv.funding_rate",
                quality_status=qualities[0] if qualities else None,
            )],
        )

    async def _dim_macro(self, obs: datetime) -> DimensionInput:
        """宏观维度：DXY 动量 + 利率方向 → SUPPORTIVE/NEUTRAL/HEADWIND/CRISIS。"""
        if self._macro is None:
            return self._gap_dimension("macro", "宏观服务未配置")

        dxy_momentum: float | None = None
        rate_now: float | None = None
        rate_prev: float | None = None
        qualities: list[str] = []
        try:
            r_now = await self._macro.get_dxy(obs)
            r_prev = await self._macro.get_dxy(obs - timedelta(days=30))
            v_now = _extract(r_now.data, "value") if r_now.success else None
            v_prev = _extract(r_prev.data, "value") if r_prev.success else None
            if v_now is not None and v_prev:
                dxy_momentum = safe_div(v_now - v_prev, v_prev, 0.0)
                qualities.append(_qstr(r_now))
        except Exception as exc:
            logger.debug(f"[regime] DXY 失败: {exc!r}")
        try:
            r_now = await self._macro.get_fed_rate(obs)
            r_prev = await self._macro.get_fed_rate(obs - timedelta(days=90))
            rate_now = _extract(r_now.data, "value") if r_now.success else None
            rate_prev = _extract(r_prev.data, "value") if r_prev.success else None
            if rate_now is not None:
                qualities.append(_qstr(r_now))
        except Exception as exc:
            logger.debug(f"[regime] Fed 利率失败: {exc!r}")

        if dxy_momentum is None and rate_now is None:
            return self._gap_dimension("macro", "DXY 与利率数据均不可用")

        score = 0.0
        n = 0
        if dxy_momentum is not None:
            # DXY 走强 → 逆风
            score += clamp(-dxy_momentum / 0.03, -1.0, 1.0)
            n += 1
        if rate_now is not None:
            if rate_prev is not None and rate_now > rate_prev + 0.01:
                score += -0.6  # 加息周期 → 逆风
            elif rate_prev is not None and rate_now < rate_prev - 0.01:
                score += 0.7   # 降息周期 → 顺风
            else:
                score += clamp((3.5 - rate_now) / 2.0, -1.0, 1.0) * 0.4
            n += 1
        raw = score / n if n else 0.0

        if raw >= 0.35:
            state, interp = "SUPPORTIVE", "宏观环境友好：美元走弱/流动性宽松，利好风险资产"
        elif raw <= -0.6:
            state, interp = "CRISIS", "宏观环境恶劣：美元急剧走强叠加利率高压，风险资产承压严重"
        elif raw <= -0.25:
            state, interp = "HEADWIND", "宏观逆风：美元走强或利率上行，压制风险资产估值"
        else:
            state, interp = "NEUTRAL", "宏观环境中性：美元与利率均无显著单边变化"

        from app.engines.regime.fusion import RegimeFusion as _F

        conf = (0.75 if n == 2 else 0.5) * (
            sum(quality_coefficient(q) for q in qualities) / len(qualities) if qualities else 0.5
        )
        return DimensionInput(
            key="macro",
            value=_F.state_to_scalar("macro", state),
            state=state,
            quality_status=qualities[0] if qualities else "ESTIMATED",
            confidence=conf,
            evidence=[EngineEvidence(
                factor="宏观状态",
                value={"dxy_momentum_30d": dxy_momentum, "fed_rate": rate_now,
                       "fed_rate_90d_ago": rate_prev},
                interpretation=interp,
                weight=self._fusion.weights.get("macro", 0.10),
                supports="bullish" if raw > 0.15 else ("bearish" if raw < -0.15 else "neutral"),
                confidence=conf,
                data_source="macro_service",
                indicator_code="macro.dxy",
                quality_status=qualities[0] if qualities else None,
            )],
        )

    async def _dim_sentiment(self, obs: datetime) -> DimensionInput:
        """情绪维度：Fear & Greed → EXTREME_FEAR/FEAR/NEUTRAL/GREED/EXTREME_GREED。"""
        if self._sentiment is None:
            return self._gap_dimension("sentiment", "情绪服务未配置")
        try:
            r = await self._sentiment.get_fear_greed(obs)
        except Exception as exc:
            return self._gap_dimension("sentiment", str(exc))
        if not r.success:
            return self._gap_dimension("sentiment", r.error or "获取失败")
        fg = _extract(r.data, "value", "normalized_value")
        if fg is None:
            return self._gap_dimension("sentiment", "恐惧贪婪指数为空")

        if fg >= 75:
            state, interp = "EXTREME_GREED", f"恐惧贪婪指数 {fg:.0f}：市场极度贪婪，情绪过热"
        elif fg >= 55:
            state, interp = "GREED", f"恐惧贪婪指数 {fg:.0f}：市场情绪偏贪婪"
        elif fg <= 25:
            state, interp = "EXTREME_FEAR", f"恐惧贪婪指数 {fg:.0f}：市场极度恐惧，情绪冰点"
        elif fg <= 45:
            state, interp = "FEAR", f"恐惧贪婪指数 {fg:.0f}：市场情绪偏恐惧"
        else:
            state, interp = "NEUTRAL", f"恐惧贪婪指数 {fg:.0f}：市场情绪中性"

        from app.engines.regime.fusion import RegimeFusion as _F

        conf = 0.8 * quality_coefficient(_qstr(r))
        return DimensionInput(
            key="sentiment",
            value=_F.state_to_scalar("sentiment", state),
            state=state,
            quality_status=_qstr(r),
            confidence=conf,
            evidence=[EngineEvidence(
                factor="情绪状态",
                value={"fear_greed": fg, "label": (r.data or {}).get("label") if isinstance(r.data, dict) else None},
                interpretation=interp,
                weight=self._fusion.weights.get("sentiment", 0.10),
                supports="bullish" if fg > 55 else ("bearish" if fg < 45 else "neutral"),
                confidence=conf,
                data_source="sentiment_service",
                indicator_code="sentiment.fear_greed",
                quality_status=_qstr(r),
            )],
        )

    # ---- 变化追踪 ----

    async def _detect_change(
        self, result: FusionResult, obs: datetime
    ) -> tuple[str | None, bool, dict[str, Any]]:
        """与上一快照比较，返回 (前一 regime, 是否变化, 变化原因)。"""
        try:
            async with get_db_session_ctx() as session:
                stmt = (
                    select(MarketRegime)
                    .where(MarketRegime.observation_time < obs)
                    .order_by(MarketRegime.observation_time.desc())
                    .limit(1)
                )
                prev = (await session.execute(stmt)).scalar_one_or_none()
        except Exception as exc:
            logger.debug(f"[regime] 查询前快照失败: {exc!r}")
            return None, False, {}

        if prev is None:
            return None, False, {}

        prev_label = prev.overall_regime
        is_changed = prev_label != result.label
        reason: dict[str, Any] = {}
        if is_changed:
            prev_details = prev.dimension_details or {}
            prev_scalars = prev_details.get("dimension_scalars", {})
            shifted = {
                k: {"from": prev_scalars.get(k), "to": result.dimension_scalars.get(k)}
                for k in set(prev_scalars) | set(result.dimension_scalars)
                if abs(
                    (result.dimension_scalars.get(k) or 0.0) - (prev_scalars.get(k) or 0.0)
                ) > 0.3
            }
            reason = {
                "from": prev_label,
                "to": result.label,
                "score_from": float(prev.regime_score) if prev.regime_score else None,
                "score_to": result.regime_score,
                "shifted_dimensions": shifted,
            }
        return prev_label, is_changed, reason

    # ---- 持久化 ----

    def _build_row(self, output: EngineOutput, obs_time: datetime) -> dict[str, Any]:
        """EngineOutput → MarketRegime 行字典。"""
        supporting, opposing = output.evidence_dicts()
        states = output.metadata.get("dimension_states", {})
        sub = output.metadata.get("sub_engine_outputs", {})

        def _enum(cls: Any, value: Any, default: Any) -> Any:
            try:
                return cls(value) if value else default
            except ValueError:
                return default

        return {
            "observation_time": obs_time,
            "trend_state": states.get("trend", "NEUTRAL")[:30],
            "valuation_state": _enum(ValuationLevel, sub.get("valuation"), ValuationLevel.FAIR),
            "capital_flow_state": states.get("capital_flow", "NEUTRAL")[:30],
            "onchain_state": states.get("onchain", "NEUTRAL")[:30],
            "derivative_state": states.get("derivative", "NEUTRAL")[:30],
            "macro_state": states.get("macro", "NEUTRAL")[:30],
            "sentiment_state": states.get("sentiment", "NEUTRAL")[:30],
            "risk_state": _enum(RiskLevel, sub.get("risk"), RiskLevel.MODERATE),
            "cycle_state": _enum(CyclePhase, sub.get("cycle"), CyclePhase.UPTREND),
            "overall_regime": output.state[:50],
            "confidence": Decimal(str(round(output.confidence, 4))),
            "regime_score": Decimal(str(round(output.score, 4))),
            "is_changed": bool(output.metadata.get("is_changed")),
            "previous_regime": output.metadata.get("previous_regime"),
            "change_reason": output.metadata.get("change_reason") or {},
            "dimension_details": {
                "dimension_scalars": output.dimension_scores,
                "dimension_states": states,
                "consistency": output.metadata.get("consistency"),
                "divergence_flag": output.metadata.get("divergence_flag"),
                "corrections": output.metadata.get("corrections", []),
                "data_gaps": output.data_gaps,
                "label_cn": output.metadata.get("label_cn"),
            },
            "evidence_summary": {
                "supporting": supporting[:6],
                "opposing": opposing[:4],
            },
            "description": output.explanation or None,
            "quality_status": self._to_quality_enum(output.data_quality),
        }

    def _row_to_output(self, row: MarketRegime) -> EngineOutput:
        """MarketRegime 行 → EngineOutput（历史回放用，as-run 语义）。"""
        details = row.dimension_details or {}
        summary = row.evidence_summary or {}

        def _restore(items: Any) -> list[EngineEvidence]:
            return [
                EngineEvidence(
                    factor=d.get("factor", ""),
                    value=d.get("value"),
                    interpretation=d.get("interpretation", ""),
                    weight=d.get("weight", 0.0),
                    supports=d.get("supports", "neutral"),
                    confidence=d.get("confidence", 0.0),
                    data_source=d.get("data_source", ""),
                    historical_percentile=d.get("historical_percentile"),
                    indicator_code=d.get("indicator_code"),
                    quality_status=d.get("quality_status"),
                )
                for d in (items or [])
                if isinstance(d, dict)
            ]

        return EngineOutput(
            state=row.overall_regime,
            confidence=float(row.confidence) if row.confidence else 0.0,
            supporting_evidence=_restore(summary.get("supporting")),
            opposing_evidence=_restore(summary.get("opposing")),
            score=float(row.regime_score) if row.regime_score else 0.0,
            observation_time=row.observation_time,
            data_quality=(
                row.quality_status.value
                if hasattr(row.quality_status, "value")
                else str(row.quality_status)
            ),
            explanation=row.description or "",
            dimension_scores=details.get("dimension_scalars", {}),
            data_gaps=details.get("data_gaps", []),
            metadata={
                "label_cn": details.get("label_cn"),
                "dimension_states": details.get("dimension_states", {}),
                "consistency": details.get("consistency"),
                "divergence_flag": details.get("divergence_flag"),
                "corrections": details.get("corrections", []),
                "previous_regime": row.previous_regime,
                "is_changed": bool(row.is_changed),
                "change_reason": row.change_reason or {},
            },
        )

    async def _emit_change_signal(
        self, output: EngineOutput, prev_regime: str, reason: dict[str, Any]
    ) -> None:
        """Regime 变化时写入 signals 表（type=REGIME_CHANGE，§4.3）。"""
        try:
            from app.models.engine import Signal

            label_cn = output.metadata.get("label_cn", output.state)
            score = output.score
            direction = (
                SignalDirection.BULLISH if score > 0.2
                else SignalDirection.BEARISH if score < -0.2
                else SignalDirection.NEUTRAL
            )
            supporting, _ = output.evidence_dicts()
            async with get_db_session_ctx() as session:
                session.add(Signal(
                    signal_time=output.observation_time,
                    signal_type="REGIME_CHANGE",
                    signal_direction=direction,
                    strength=Decimal(str(round(clamp(abs(score)), 4))),
                    category="REGIME",
                    engine_source="REGIME_ENGINE",
                    trigger_conditions=[{
                        "type": "regime_change",
                        "before": prev_regime, "after": output.state,
                        "regime_score": score,
                    }],
                    evidence=supporting[:5],
                    market_context={
                        "dimension_states": output.metadata.get("dimension_states", {}),
                        "change_reason": reason,
                    },
                    title=f"市场状态变化: {prev_regime} → {label_cn}",
                    description=f"Market regime changed from {prev_regime} to {output.state}",
                    description_cn=output.explanation or f"综合市场状态由「{prev_regime}」转为「{label_cn}」",
                    is_active=True,
                ))
            logger.info(f"[regime] 状态变化信号已写入: {prev_regime} → {output.state}")
        except Exception as exc:
            logger.warning(f"[regime] 写入状态变化信号失败: {exc!r}")

    # ---- 辅助 ----

    @staticmethod
    def _neutral_evidence(result: FusionResult) -> EngineEvidence:
        """全维度降级时的兜底证据。"""
        return EngineEvidence(
            factor="综合融合",
            value={"regime_score": result.regime_score},
            interpretation="全部维度数据不可用，输出为中性兜底判断（置信度极低）",
            weight=1.0,
            supports="neutral",
            confidence=0.1,
            data_source="regime_engine",
            quality_status="INVALID",
        )

    @staticmethod
    def _to_quality_enum(status: str) -> QualityStatus:
        try:
            return QualityStatus(status)
        except ValueError:
            return QualityStatus.VERIFIED

    @staticmethod
    def _overall_quality(evidence_list: list[EngineEvidence]) -> str:
        order = ["VERIFIED", "ESTIMATED", "STALE", "CONFLICT", "INVALID"]
        worst = "VERIFIED"
        for ev in evidence_list:
            if ev.confidence <= 0:
                continue
            status = ev.quality_status or "VERIFIED"
            if status in order and order.index(status) > order.index(worst):
                worst = status
        return worst


# --------------------------------------------------------------------------- #
# 全局单例
# --------------------------------------------------------------------------- #

_regime_engine: MarketRegimeEngine | None = None


def get_regime_engine() -> MarketRegimeEngine | None:
    """获取全局 MarketRegimeEngine 单例（未初始化时返回 None）。"""
    return _regime_engine


def set_regime_engine(engine: MarketRegimeEngine) -> None:
    """设置全局 MarketRegimeEngine 单例（应用启动时注入 Service/子引擎后调用）。"""
    global _regime_engine
    _regime_engine = engine
