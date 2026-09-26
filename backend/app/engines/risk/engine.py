"""RiskEngine — 风险引擎（架构文档 §3）。

11 个风险因子（0-100）→ 聚合为 7 个输出维度 → overall_risk 综合评分。

RiskLevel 映射：
- 0-15:   VERY_LOW
- 15-30:  LOW
- 30-50:  MODERATE
- 50-70:  HIGH
- 70-85:  VERY_HIGH
- 85-100: EXTREME

最大值限制规则（防止单因子极端被平均稀释）：
- 任一因子 > 90 → overall 至少 HIGH（50）
- 3 个以上因子 > 70 → overall 至少 VERY_HIGH（70）

红线：只输出风险描述，不输出 BUY/SELL；数据缺失显式降级。
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
    normalize_weights,
)
from app.engines.risk.factors import (
    FACTOR_TO_DIMENSION,
    RISK_DIMENSION_WEIGHTS,
    calculate_crowding_risk,
    calculate_drawdown_risk,
    calculate_funding_risk,
    calculate_leverage_risk,
    calculate_liquidation_risk,
    calculate_liquidity_risk,
    calculate_macro_risk,
    calculate_oi_risk,
    calculate_onchain_risk,
    calculate_valuation_risk,
    calculate_volatility_risk,
)
from app.models.engine import RiskScore
from app.models.enums import QualityStatus, RiskLevel

__all__ = [
    "RiskEngine",
    "get_risk_engine",
    "set_risk_engine",
    "score_to_risk_level",
]


#: overall_risk → RiskLevel 阈值（升序）
RISK_LEVEL_THRESHOLDS: list[tuple[float, RiskLevel]] = [
    (15.0, RiskLevel.VERY_LOW),
    (30.0, RiskLevel.LOW),
    (50.0, RiskLevel.MODERATE),
    (70.0, RiskLevel.HIGH),
    (85.0, RiskLevel.VERY_HIGH),
    (float("inf"), RiskLevel.EXTREME),
]

#: 最大值限制规则的等级下限
FLOOR_HIGH = 50.0        # 任一因子 > 90 → 至少 HIGH
FLOOR_VERY_HIGH = 70.0   # 3 个以上因子 > 70 → 至少 VERY_HIGH

#: 等级中文名与解释模板
RISK_LEVEL_LABELS: dict[str, tuple[str, str]] = {
    "VERY_LOW": ("很低", "当前各项风险指标均处于低位，市场环境相对平静"),
    "LOW": ("较低", "当前风险指标整体偏低，仅少数维度需要留意"),
    "MODERATE": ("中等", "当前存在中等程度的风险，部分维度指标偏高"),
    "HIGH": ("较高", "当前风险水平较高，多个维度出现警示信号"),
    "VERY_HIGH": ("很高", "当前风险水平很高，大部分风险维度处于警戒区域"),
    "EXTREME": ("极端", "当前风险处于极端水平，历史上类似状态往往伴随剧烈波动"),
}


def score_to_risk_level(score: float) -> RiskLevel:
    """综合风险评分（0-100）→ RiskLevel。"""
    for threshold, level in RISK_LEVEL_THRESHOLDS:
        if score < threshold:
            return level
    return RiskLevel.EXTREME


class RiskEngine(EngineBase):
    """风险引擎。

    Usage::

        engine = RiskEngine(
            market_service=..., derivatives_service=..., onchain_service=...,
            macro_service=..., sentiment_service=..., valuation_engine=...,
        )
        output = await engine.calculate()   # state=RiskLevel 枚举值, score=0-100
    """

    def __init__(
        self,
        *,
        market_service: Any = None,
        derivatives_service: Any = None,
        onchain_service: Any = None,
        macro_service: Any = None,
        sentiment_service: Any = None,
        valuation_engine: Any = None,
        dimension_weights: dict[str, float] | None = None,
        persist: bool = True,
    ) -> None:
        self._market = market_service
        self._derivatives = derivatives_service
        self._onchain = onchain_service
        self._macro = macro_service
        self._sentiment = sentiment_service
        self._valuation_engine = valuation_engine
        self._dim_weights = dict(dimension_weights or RISK_DIMENSION_WEIGHTS)
        self._persist = persist

    @property
    def name(self) -> str:
        return "risk"

    @property
    def _orm_model(self) -> type:
        return RiskScore

    # ---- 主计算 ----

    async def calculate(self, as_of: datetime | None = None) -> EngineOutput:
        """计算当前（或指定时刻）的风险状态。"""
        obs = self._to_utc(as_of)
        logger.info(f"[risk] 开始计算风险 as_of={obs.isoformat()}")

        # 1. 估值引擎输出（valuation_risk 因子的输入）
        valuation_output = None
        if self._valuation_engine is not None:
            try:
                valuation_output = await self._valuation_engine.calculate(as_of=obs)
            except Exception as exc:
                logger.warning(f"[risk] 估值引擎调用失败（估值风险因子降级）: {exc!r}")

        # 2. 并发计算 11 个因子
        scores, evidence_list, details_map, data_gaps = await self._compute_factors(
            obs, valuation_output
        )

        # 3. 聚合为 7 个维度分
        dimension_scores = self._aggregate_dimensions(scores, evidence_list)

        # 4. overall = 维度加权平均 + 最大值限制
        overall = self._overall_score(dimension_scores)
        floor = self._apply_max_floor(scores)
        warnings: list[str] = []
        if floor is not None:
            floor_score, note = floor
            if floor_score > overall:
                overall = floor_score
            warnings.append(note)

        # 5. 映射等级（含下限抬升后的评分）
        level = score_to_risk_level(overall)

        # 6. 前一等级
        prev_level = await self._load_previous_level(obs)

        # 7. 构建输出
        cn, template = RISK_LEVEL_LABELS.get(level.value, (level.value, ""))
        explanation = f"综合风险等级「{cn}」（评分 {overall:.0f}/100）：{template}"
        top_risks = sorted(
            ((k, v) for k, v in scores.items() if v is not None),
            key=lambda kv: kv[1], reverse=True,
        )[:3]
        if top_risks:
            explanation += "。当前最突出的风险因子：" + "、".join(
                f"{self._factor_cn(k)}（{v:.0f} 分）" for k, v in top_risks
            )
        if warnings:
            explanation += f"。{warnings[0]}"
        if data_gaps:
            explanation += f"。注意：{len(data_gaps)} 个因子数据缺失，评估置信度已下调"

        output = self._build_output(
            state=level.value,
            score=overall,
            evidence=evidence_list,
            explanation=explanation,
            observation_time=obs,
            data_quality=self._overall_quality(evidence_list),
            dimension_scores={k: round(v, 2) for k, v in dimension_scores.items()},
            data_gaps=data_gaps,
            metadata={
                "factor_scores": {k: v for k, v in scores.items() if v is not None},
                "details": details_map,
                "warnings": warnings,
                "previous_level": prev_level,
            },
            base_confidence=0.8,
        )
        logger.info(
            f"[risk] 计算完成: level={level.value} overall={overall:.1f} "
            f"confidence={output.confidence:.2f} gaps={len(data_gaps)}"
        )

        if self._persist:
            await self.save_result(output)
        return output

    async def calculate_historical(
        self, start: datetime, end: datetime
    ) -> list[EngineOutput]:
        """按日计算历史风险序列。"""
        outputs: list[EngineOutput] = []
        day = self._to_utc(start)
        end_utc = self._to_utc(end)
        while day <= end_utc:
            try:
                out = await self.calculate(as_of=day)
                outputs.append(out)
            except Exception as exc:
                logger.error(f"[risk] 历史计算失败 {day.date()}: {exc!r}")
            day += timedelta(days=1)
        logger.info(f"[risk] 历史计算完成: {len(outputs)} 天")
        return outputs

    # ---- 因子计算 ----

    async def _compute_factors(
        self, obs: datetime, valuation_output: Any
    ) -> tuple[dict[str, float | None], list[EngineEvidence], dict[str, Any], list[str]]:
        """并发计算 11 个风险因子。"""
        tasks: dict[str, Any] = {
            "valuation_risk": calculate_valuation_risk(valuation_output),
            "volatility_risk": calculate_volatility_risk(self._market, obs),
            "leverage_risk": calculate_leverage_risk(self._derivatives, obs),
            "funding_risk": calculate_funding_risk(self._derivatives, obs),
            "oi_risk": calculate_oi_risk(self._derivatives, self._market, obs),
            "liquidation_risk": calculate_liquidation_risk(self._derivatives, obs),
            "liquidity_risk": calculate_liquidity_risk(self._market, obs),
            "macro_risk": calculate_macro_risk(self._macro, obs),
            "onchain_risk": calculate_onchain_risk(self._onchain, obs),
            "crowding_risk": calculate_crowding_risk(
                self._derivatives, self._sentiment, obs
            ),
            "drawdown_risk": calculate_drawdown_risk(self._market, obs),
        }

        async def _run(key: str, coro: Any) -> tuple[str, Any]:
            try:
                return key, await coro
            except Exception as exc:
                logger.warning(f"[risk] 因子 {key} 计算异常: {exc!r}")
                return key, exc

        results = await asyncio.gather(*[_run(k, t) for k, t in tasks.items()])

        scores: dict[str, float | None] = {}
        evidence_list: list[EngineEvidence] = []
        details_map: dict[str, Any] = {}
        data_gaps: list[str] = []
        for key, res in results:
            if isinstance(res, BaseException):
                scores[key] = None
                data_gaps.append(key)
                evidence_list.append(self._missing_evidence(self._factor_cn(key), str(res)))
                continue
            score, ev, details = res
            scores[key] = score
            evidence_list.append(ev)
            if details:
                details_map[key] = details
            if score is None:
                data_gaps.append(key)

        return scores, evidence_list, details_map, data_gaps

    @staticmethod
    def _missing_evidence(factor: str, reason: str) -> EngineEvidence:
        return EngineEvidence(
            factor=factor,
            value=None,
            interpretation=f"{factor}数据暂不可用（{reason}），不参与本次风险评估",
            weight=0.0,
            supports="neutral",
            confidence=0.0,
            data_source="risk_engine",
            quality_status="INVALID",
        )

    # ---- 聚合 ----

    def _aggregate_dimensions(
        self, scores: dict[str, float | None], evidence_list: list[EngineEvidence]
    ) -> dict[str, float]:
        """11 因子 → 7 维度（6 计算维度 + overall 另行计算）。

        维度分 = 归属因子的等权平均（跳过缺失因子）；
        volatility/drawdown → trend_risk；funding/oi/leverage → leverage_risk；
        liquidation/liquidity/crowding → liquidity_risk。
        """
        conf_by_label = {ev.factor: max(ev.confidence, 0.0) for ev in evidence_list}
        buckets: dict[str, list[tuple[float, float]]] = {}
        for factor_key, dim in FACTOR_TO_DIMENSION.items():
            score = scores.get(factor_key)
            if score is None:
                continue
            conf = conf_by_label.get(self._factor_cn(factor_key), 0.5)
            buckets.setdefault(dim, []).append((score, max(conf, 0.1)))

        dims: dict[str, float] = {}
        for dim, items in buckets.items():
            total_w = sum(w for _, w in items)
            dims[dim] = sum(s * w for s, w in items) / total_w if total_w else 0.0
        # 缺失维度按中性 50 补齐（并已在 data_gaps 中声明）
        for dim in self._dim_weights:
            dims.setdefault(dim, 50.0)
        # 输出维度包含全部 7 项（overall 由 _overall_score 填充）
        return dims

    def _overall_score(self, dimension_scores: dict[str, float]) -> float:
        """overall = 6 维度加权平均（权重见 RISK_DIMENSION_WEIGHTS）。"""
        weights = normalize_weights(self._dim_weights)
        num = 0.0
        den = 0.0
        for dim, w in weights.items():
            if dim in dimension_scores:
                num += dimension_scores[dim] * w
                den += w
        return clamp(num / den if den > 0 else 50.0, 0.0, 100.0)

    @staticmethod
    def _apply_max_floor(
        scores: dict[str, float | None]
    ) -> tuple[float, str] | None:
        """最大值限制规则（任务规格）。

        Returns:
            (下限分, 警告文案) 或 None（未触发）。
        """
        valid = {k: v for k, v in scores.items() if v is not None}
        extreme = [k for k, v in valid.items() if v > 90]
        elevated = [k for k, v in valid.items() if v > 70]

        if extreme:
            names = "、".join(RiskEngine._factor_cn(k) for k in extreme[:3])
            return FLOOR_HIGH, f"单项风险因子极端（{names} > 90 分），综合等级至少为 HIGH"
        if len(elevated) >= 3:
            names = "、".join(RiskEngine._factor_cn(k) for k in elevated[:4])
            return FLOOR_VERY_HIGH, (
                f"{len(elevated)} 个风险因子同时偏高（{names} > 70 分），综合等级至少为 VERY_HIGH"
            )
        return None

    @staticmethod
    def _factor_cn(key: str) -> str:
        """因子键 → 中文名。"""
        return {
            "valuation_risk": "估值风险", "volatility_risk": "波动率风险",
            "leverage_risk": "杠杆风险", "funding_risk": "资金费率风险",
            "oi_risk": "OI 变化风险", "liquidation_risk": "清算风险",
            "liquidity_risk": "流动性风险", "macro_risk": "宏观风险",
            "onchain_risk": "链上风险", "crowding_risk": "拥挤度风险",
            "drawdown_risk": "回撤风险",
        }.get(key, key)

    # ---- 持久化 ----

    async def _load_previous_level(self, obs: datetime) -> str | None:
        try:
            async with get_db_session_ctx() as session:
                stmt = (
                    select(RiskScore.risk_level)
                    .where(RiskScore.observation_time < obs)
                    .order_by(RiskScore.observation_time.desc())
                    .limit(1)
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is None:
                    return None
                return row.value if hasattr(row, "value") else str(row)
        except Exception as exc:
            logger.debug(f"[risk] 查询前一等级失败: {exc!r}")
            return None

    def _build_row(self, output: EngineOutput, obs_time: datetime) -> dict[str, Any]:
        """EngineOutput → RiskScore 行字典。"""
        supporting, opposing = output.evidence_dicts()
        dims = output.dimension_scores
        factors = output.metadata.get("factor_scores", {})
        prev = output.metadata.get("previous_level")

        def _d(v: Any) -> Decimal:
            return Decimal(str(round(float(v or 0), 2)))

        return {
            "observation_time": obs_time,
            "overall_risk": _d(output.score),
            "trend_risk": _d(dims.get("trend_risk")),
            "valuation_risk": _d(dims.get("valuation_risk")),
            "leverage_risk": _d(dims.get("leverage_risk")),
            "liquidity_risk": _d(dims.get("liquidity_risk")),
            "macro_risk": _d(dims.get("macro_risk")),
            "onchain_risk": _d(dims.get("onchain_risk")),
            "volatility_risk": _d(factors.get("volatility_risk")),
            "crowding_risk": _d(factors.get("crowding_risk")),
            "drawdown_risk": _d(factors.get("drawdown_risk")),
            "risk_level": RiskLevel(output.state),
            "previous_level": RiskLevel(prev) if prev else None,
            "confidence": Decimal(str(round(output.confidence, 4))),
            "risk_factors": [
                {"factor": k, "score": v, "dimension": FACTOR_TO_DIMENSION.get(k)}
                for k, v in factors.items()
            ],
            "evidence": supporting + opposing,
            "warnings": output.metadata.get("warnings", []),
            "description": output.explanation or None,
            "quality_status": self._to_quality_enum(output.data_quality),
        }

    def _row_to_output(self, row: RiskScore) -> EngineOutput:
        """RiskScore 行 → EngineOutput。"""
        level = row.risk_level.value if hasattr(row.risk_level, "value") else str(row.risk_level)
        evidence = [
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
            for d in (row.evidence or [])
            if isinstance(d, dict)
        ]
        # 风险引擎语义：高风险证据 = bearish 方向 → 与高风险等级同向为 supporting
        level_dir = "bearish" if level in ("HIGH", "VERY_HIGH", "EXTREME") else (
            "bullish" if level in ("VERY_LOW", "LOW") else "neutral"
        )
        supporting = [e for e in evidence if e.supports == level_dir or e.supports == "neutral"]
        opposing = [e for e in evidence if e not in supporting]
        dims = {
            "trend_risk": float(row.trend_risk or 0),
            "valuation_risk": float(row.valuation_risk or 0),
            "leverage_risk": float(row.leverage_risk or 0),
            "liquidity_risk": float(row.liquidity_risk or 0),
            "macro_risk": float(row.macro_risk or 0),
            "onchain_risk": float(row.onchain_risk or 0),
        }
        return EngineOutput(
            state=level,
            confidence=float(row.confidence) if row.confidence else 0.0,
            supporting_evidence=supporting,
            opposing_evidence=opposing,
            score=float(row.overall_risk) if row.overall_risk else 0.0,
            observation_time=row.observation_time,
            data_quality=(
                row.quality_status.value
                if hasattr(row.quality_status, "value")
                else str(row.quality_status)
            ),
            explanation=row.description or "",
            dimension_scores=dims,
            metadata={
                "warnings": row.warnings or [],
                "factor_scores": {
                    rf.get("factor"): rf.get("score")
                    for rf in (row.risk_factors or [])
                    if isinstance(rf, dict)
                },
                "previous_level": (
                    row.previous_level.value
                    if hasattr(row.previous_level, "value")
                    else row.previous_level
                ),
            },
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

    @staticmethod
    def _to_utc(dt: datetime | None) -> datetime:
        if dt is None:
            return datetime.now(timezone.utc)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# 全局单例
# --------------------------------------------------------------------------- #

_risk_engine: RiskEngine | None = None


def get_risk_engine() -> RiskEngine | None:
    """获取全局 RiskEngine 单例（未初始化时返回 None）。"""
    return _risk_engine


def set_risk_engine(engine: RiskEngine) -> None:
    """设置全局 RiskEngine 单例（应用启动时注入 Service 后调用）。"""
    global _risk_engine
    _risk_engine = engine
