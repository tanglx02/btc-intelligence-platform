"""ValuationEngine — 估值引擎（架构文档 §2）。

5 个估值维度加权融合为 0-100 综合评分，映射到 5 级估值状态：
- 0-20:  DEEP_UNDERVALUED（深度低估）
- 20-40: UNDERVALUED（低估）
- 40-60: FAIR（合理）
- 60-80: OVERVALUED（高估）
- 80-100: EXTREME_OVERVALUED（极度高估）

重要约束（红线）：
- 估值状态只描述「当前价格相对历史的高低」，绝不解释为「未来一定上涨/下跌」；
- 禁止输出 BUY/SELL；
- 数据缺失的维度自动跳过并重归一化权重，置信度相应下降，缺口显式声明。
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
from app.engines.valuation.factors import (
    VALUATION_FACTOR_WEIGHTS,
    calculate_cost_basis_dimension,
    calculate_drawdown_dimension,
    calculate_mvrv_dimension,
    calculate_realized_cap_dimension,
    calculate_trend_dimension,
)
from app.models.engine import ValuationState
from app.models.enums import QualityStatus, ValuationLevel

__all__ = [
    "ValuationEngine",
    "get_valuation_engine",
    "set_valuation_engine",
    "score_to_level",
]


#: 综合评分 → 估值等级阈值（升序）
LEVEL_THRESHOLDS: list[tuple[float, ValuationLevel]] = [
    (20.0, ValuationLevel.DEEP_UNDERVALUED),
    (40.0, ValuationLevel.UNDERVALUED),
    (60.0, ValuationLevel.FAIR),
    (80.0, ValuationLevel.OVERVALUED),
    (float("inf"), ValuationLevel.EXTREME_OVERVALUED),
]

#: 等级中文名与解释模板（注意：不含任何未来走势预测）
LEVEL_LABELS: dict[str, tuple[str, str]] = {
    "DEEP_UNDERVALUED": ("深度低估", "当前价格处于历史估值分布的极低区域，明显低于历史常态水平"),
    "UNDERVALUED": ("低估", "当前价格低于历史估值中枢，处于偏低区域"),
    "FAIR": ("合理", "当前价格与历史估值中枢相当，处于合理区间"),
    "OVERVALUED": ("高估", "当前价格高于历史估值中枢，处于偏高区域"),
    "EXTREME_OVERVALUED": ("极度高估", "当前价格处于历史估值分布的极端高位，明显偏离历史常态"),
}

#: 固定后缀——估值语义免责声明（每次输出必须携带）
VALUATION_DISCLAIMER = "估值状态仅反映当前价格相对历史的高低，不代表未来价格会必然上涨或下跌"


def score_to_level(score: float) -> ValuationLevel:
    """综合评分（0-100）→ 估值等级。"""
    for threshold, level in LEVEL_THRESHOLDS:
        if score < threshold:
            return level
    return ValuationLevel.EXTREME_OVERVALUED


class ValuationEngine(EngineBase):
    """估值引擎。

    Usage::

        engine = ValuationEngine(onchain_service=..., market_service=...)
        output = await engine.calculate()        # score ∈ [0,100], state=等级枚举值
        await engine.save_result(output)
    """

    def __init__(
        self,
        *,
        onchain_service: Any = None,
        market_service: Any = None,
        dimension_weights: dict[str, float] | None = None,
        persist: bool = True,
    ) -> None:
        """初始化 ValuationEngine。

        Args:
            onchain_service: 链上服务（MVRV/NUPL/Realized Cap 维度）
            market_service: 行情服务（价格/趋势/回撤维度）
            dimension_weights: 自定义维度权重（None 时使用默认 25/25/20/15/15）
            persist: calculate() 后是否自动持久化（默认 True）
        """
        self._onchain = onchain_service
        self._market = market_service
        self._weights = dict(dimension_weights or VALUATION_FACTOR_WEIGHTS)
        self._persist = persist

    @property
    def name(self) -> str:
        return "valuation"

    @property
    def _orm_model(self) -> type:
        return ValuationState

    @staticmethod
    def _state_direction(state: str) -> str:
        """估值语义方向：高估=对价格看跌（bearish），低估=看涨（bullish）。

        覆写基类：基类关键词表会把 OVERVALUED 当 bullish，不符合估值语义。
        """
        s = state.upper()
        if "UNDERVALUED" in s:
            return "bullish"
        if "OVERVALUED" in s:
            return "bearish"
        return "neutral"

    # ---- 主计算 ----

    async def calculate(self, as_of: datetime | None = None) -> EngineOutput:
        """计算当前（或指定时刻）的估值状态。"""
        obs = self._to_utc(as_of)
        logger.info(f"[valuation] 开始计算估值 as_of={obs.isoformat()}")

        # 1. 并发计算 5 个维度
        scores, evidence_list, details_map, data_gaps = await self._compute_dimensions(obs)

        # 2. 加权融合（跳过缺失维度，权重重归一化）
        overall = self._fuse(scores, evidence_list)

        # 3. 映射等级
        level = score_to_level(overall)

        # 4. 前一等级（用于变化追踪）
        prev_level = await self._load_previous_level(obs)

        # 5. 构建输出
        cn, template = LEVEL_LABELS.get(level.value, (level.value, ""))
        explanation = f"估值评级「{cn}」（综合评分 {overall:.0f}/100）：{template}。{VALUATION_DISCLAIMER}"
        if data_gaps:
            explanation += f"。注意：{len(data_gaps)} 个维度数据缺失（{'、'.join(data_gaps)}），判断置信度已相应下调"

        output = self._build_output(
            state=level.value,
            score=overall,
            evidence=evidence_list,
            explanation=explanation,
            observation_time=obs,
            data_quality=self._overall_quality(evidence_list),
            dimension_scores={k: v for k, v in scores.items() if v is not None},
            data_gaps=data_gaps,
            metadata={
                "details": details_map,
                "previous_level": prev_level,
                "disclaimer": VALUATION_DISCLAIMER,
            },
            base_confidence=0.8,
        )
        logger.info(
            f"[valuation] 计算完成: level={level.value} score={overall:.1f} "
            f"confidence={output.confidence:.2f} gaps={data_gaps}"
        )

        if self._persist:
            await self.save_result(output)
        return output

    async def calculate_historical(
        self, start: datetime, end: datetime
    ) -> list[EngineOutput]:
        """按日计算历史估值序列。"""
        outputs: list[EngineOutput] = []
        day = self._to_utc(start)
        end_utc = self._to_utc(end)
        while day <= end_utc:
            try:
                out = await self.calculate(as_of=day)
                outputs.append(out)
            except Exception as exc:
                logger.error(f"[valuation] 历史计算失败 {day.date()}: {exc!r}")
            day += timedelta(days=1)
        logger.info(f"[valuation] 历史计算完成: {len(outputs)} 天")
        return outputs

    # ---- 维度计算 ----

    async def _compute_dimensions(
        self, obs: datetime
    ) -> tuple[dict[str, float | None], list[EngineEvidence], dict[str, Any], list[str]]:
        """并发计算 5 个估值维度。"""
        tasks: dict[str, Any] = {}
        if self._onchain:
            tasks["mvrv"] = calculate_mvrv_dimension(self._onchain, obs)
            tasks["realized_cap"] = calculate_realized_cap_dimension(
                self._onchain, self._market, obs
            )
            tasks["cost_basis"] = calculate_cost_basis_dimension(
                self._onchain, self._market, obs
            )
        else:
            tasks["mvrv"] = tasks["realized_cap"] = tasks["cost_basis"] = None
        if self._market:
            tasks["trend"] = calculate_trend_dimension(self._market, obs)
            tasks["drawdown"] = calculate_drawdown_dimension(self._market, obs)
        else:
            tasks["trend"] = tasks["drawdown"] = None

        label_map = {
            "mvrv": "MVRV 估值维度", "realized_cap": "Realized Cap 维度",
            "cost_basis": "成本基础维度", "trend": "趋势偏离维度", "drawdown": "回撤维度",
        }

        scores: dict[str, float | None] = {}
        evidence_list: list[EngineEvidence] = []
        details_map: dict[str, Any] = {}
        data_gaps: list[str] = []

        async def _run(key: str, coro: Any) -> tuple[str, Any]:
            try:
                return key, await coro
            except Exception as exc:
                logger.warning(f"[valuation] 维度 {key} 计算异常: {exc!r}")
                return key, exc

        active = {k: t for k, t in tasks.items() if t is not None}
        results = await asyncio.gather(*[_run(k, t) for k, t in active.items()])
        resolved = dict(results)

        for key in tasks:
            if tasks[key] is None:
                scores[key] = None
                data_gaps.append(key)
                evidence_list.append(self._missing_evidence(label_map[key], "数据服务未配置"))
                continue
            res = resolved.get(key)
            if isinstance(res, BaseException):
                scores[key] = None
                data_gaps.append(key)
                evidence_list.append(self._missing_evidence(label_map[key], str(res)))
                continue
            score, ev, details = res
            ev.weight = self._weights.get(key, ev.weight)
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
            interpretation=f"{factor}数据暂不可用（{reason}），不参与本次估值判断",
            weight=0.0,
            supports="neutral",
            confidence=0.0,
            data_source="valuation_engine",
            quality_status="INVALID",
        )

    def _fuse(
        self, scores: dict[str, float | None], evidence_list: list[EngineEvidence]
    ) -> float:
        """加权融合：有效维度按 权重×置信度 归一化加权平均。"""
        conf_by_label = {ev.factor: ev.confidence for ev in evidence_list}
        label_map = {
            "mvrv": "MVRV 估值维度", "realized_cap": "Realized Cap 维度",
            "cost_basis": "成本基础维度", "trend": "趋势偏离维度", "drawdown": "回撤维度",
        }
        weights = normalize_weights(self._weights)
        num = 0.0
        den = 0.0
        for key, score in scores.items():
            if score is None:
                continue
            conf = conf_by_label.get(label_map.get(key, key), 0.0)
            w = weights.get(key, 0.0) * max(conf, 0.1)
            if w <= 0:
                continue
            num += score * w
            den += w
        return clamp(num / den if den > 0 else 50.0, 0.0, 100.0)

    # ---- 持久化 ----

    async def _load_previous_level(self, obs: datetime) -> str | None:
        """查询 obs 之前最近的估值等级。"""
        try:
            async with get_db_session_ctx() as session:
                stmt = (
                    select(ValuationState.valuation_level)
                    .where(ValuationState.observation_time < obs)
                    .order_by(ValuationState.observation_time.desc())
                    .limit(1)
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is None:
                    return None
                return row.value if hasattr(row, "value") else str(row)
        except Exception as exc:
            logger.debug(f"[valuation] 查询前一等级失败: {exc!r}")
            return None

    def _build_row(self, output: EngineOutput, obs_time: datetime) -> dict[str, Any]:
        """EngineOutput → ValuationState 行字典。"""
        supporting, opposing = output.evidence_dicts()
        details = output.metadata.get("details", {})
        mvrv_d = details.get("mvrv", {})
        cost_d = details.get("cost_basis", {})
        rc_d = details.get("realized_cap", {})
        prev = output.metadata.get("previous_level")

        def _dec(v: Any, places: int = 6) -> Decimal | None:
            if v is None:
                return None
            try:
                return Decimal(str(round(float(v), places)))
            except (TypeError, ValueError):
                return None

        # 成本基础近似价格 = price / cost_ratio（NUPL 还原）
        cost_basis_price = None
        if rc_d.get("price") and cost_d.get("cost_ratio"):
            cost_basis_price = rc_d["price"] / cost_d["cost_ratio"]

        return {
            "observation_time": obs_time,
            "valuation_level": ValuationLevel(output.state),
            "previous_level": ValuationLevel(prev) if prev else None,
            "confidence": Decimal(str(round(output.confidence, 4))),
            "mvrv_value": _dec(mvrv_d.get("mvrv")),
            "mvrv_percentile": _dec(mvrv_d.get("mvrv_percentile"), 4),
            "realized_cap": _dec(rc_d.get("realized_cap"), 2),
            "realized_price": None,
            "cost_basis": _dec(cost_basis_price, 8),
            "nupl_value": _dec(cost_d.get("nupl")),
            "nupl_percentile": _dec(cost_d.get("nupl_percentile"), 4),
            "overall_score": Decimal(str(round(output.score, 4))),
            "components": {
                "dimension_scores": output.dimension_scores,
                "details": details,
                "data_gaps": output.data_gaps,
            },
            "evidence": supporting + opposing,
            "historical_context": {
                "score_to_level": {lv.value: th for th, lv in LEVEL_THRESHOLDS if th != float("inf")},
                "disclaimer": VALUATION_DISCLAIMER,
            },
            "description": output.explanation or None,
            "quality_status": self._to_quality_enum(output.data_quality),
        }

    def _row_to_output(self, row: ValuationState) -> EngineOutput:
        """ValuationState 行 → EngineOutput。"""
        level = (
            row.valuation_level.value
            if hasattr(row.valuation_level, "value")
            else str(row.valuation_level)
        )
        components = row.components or {}
        evidence_dicts = row.evidence or []
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
            for d in evidence_dicts
            if isinstance(d, dict)
        ]
        # 按等级方向分离支持/反对方（高估=bearish，低估=bullish）
        level_dir = self._state_direction(level)
        supporting = [
            e for e in evidence
            if e.supports == level_dir or e.supports == "neutral"
        ]
        opposing = [e for e in evidence if e not in supporting]
        return EngineOutput(
            state=level,
            confidence=float(row.confidence) if row.confidence else 0.0,
            supporting_evidence=supporting,
            opposing_evidence=opposing,
            score=float(row.overall_score) if row.overall_score else 0.0,
            observation_time=row.observation_time,
            data_quality=(
                row.quality_status.value
                if hasattr(row.quality_status, "value")
                else str(row.quality_status)
            ),
            explanation=row.description or "",
            dimension_scores=components.get("dimension_scores", {}),
            data_gaps=components.get("data_gaps", []),
            metadata={"details": components.get("details", {}),
                      "previous_level": (
                          row.previous_level.value
                          if hasattr(row.previous_level, "value")
                          else row.previous_level
                      )},
        )

    @staticmethod
    def _to_quality_enum(status: str) -> QualityStatus:
        try:
            return QualityStatus(status)
        except ValueError:
            return QualityStatus.VERIFIED

    @staticmethod
    def _overall_quality(evidence_list: list[EngineEvidence]) -> str:
        """整体数据质量：取有效证据中最差的质量状态。"""
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

_valuation_engine: ValuationEngine | None = None


def get_valuation_engine() -> ValuationEngine | None:
    """获取全局 ValuationEngine 单例（未初始化时返回 None）。"""
    return _valuation_engine


def set_valuation_engine(engine: ValuationEngine) -> None:
    """设置全局 ValuationEngine 单例（应用启动时注入 Service 后调用）。"""
    global _valuation_engine
    _valuation_engine = engine
