"""CycleEngine — 市场周期引擎（架构文档 §1）。

三层融合识别 BTC 当前所处市场阶段：
1. **多因子加权打分**：7 个因子（价格趋势/链上/资金流/衍生品/宏观/情绪/估值锚）
   独立评分 -1~+1，加权融合为综合得分，映射到 9 个阶段；
2. **规则引擎**：少量高置信守卫规则（VETO/PROMOTE），触发时进入证据链；
3. **历史模式匹配**：PatternMatcher 检索 Top-N 相似历史时段，做先验修正。

防抖状态机（§1.5）：
- 合法转换路径约束（不允许跳跃，如 DEEP_BEAR → ACCELERATION）；
- 最短持续期 7 天（TOP_RISK 例外 3 天）；
- 跨阶段转换需连续 3 日确认（TOP_RISK/DECLINE 进入仅需 1 日）；
- 非法转换输出「过渡期」标记并降低置信度。

红线：禁止输出 BUY/SELL；禁止减半日期硬编码；数据缺失显式声明 data_gaps。
"""

from __future__ import annotations

import asyncio
import math
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
)
from app.engines.cycle.factors import (
    CYCLE_FACTOR_WEIGHTS,
    calculate_capital_flow_factor,
    calculate_derivative_factor,
    calculate_macro_factor,
    calculate_onchain_factor,
    calculate_price_trend_factor,
    calculate_sentiment_factor,
    calculate_valuation_anchor_factor,
)
from app.engines.cycle.patterns import PatternMatcher, SimilarPeriod
from app.models.engine import CycleState
from app.models.enums import CyclePhase, QualityStatus, SignalDirection

__all__ = [
    "CycleEngine",
    "get_cycle_engine",
    "set_cycle_engine",
    "LEGAL_TRANSITIONS",
    "score_to_phase",
]


# --------------------------------------------------------------------------- #
# 阶段映射与状态机常量
# --------------------------------------------------------------------------- #

#: 综合得分 → 基础阶段的阈值区间（得分升序，架构文档 §1.3 第一层的简化实现）
PHASE_THRESHOLDS: list[tuple[float, str]] = [
    (-0.70, CyclePhase.DEEP_BEAR.value),
    (-0.40, CyclePhase.BEAR.value),
    (-0.15, CyclePhase.BOTTOM_BUILDING.value),
    (0.15, CyclePhase.RECOVERY.value),
    (0.45, CyclePhase.UPTREND.value),
    (float("inf"), CyclePhase.ACCELERATION.value),
]

#: 合法转换路径（§1.5 mermaid 图；同阶段保持视为合法）
LEGAL_TRANSITIONS: dict[str, set[str]] = {
    "DEEP_BEAR": {"DEEP_BEAR", "BEAR"},
    "BEAR": {"BEAR", "DEEP_BEAR", "BOTTOM_BUILDING"},
    "BOTTOM_BUILDING": {"BOTTOM_BUILDING", "BEAR", "RECOVERY"},
    "RECOVERY": {"RECOVERY", "BOTTOM_BUILDING", "UPTREND", "BEAR"},
    "UPTREND": {"UPTREND", "ACCELERATION", "RECOVERY", "DECLINE"},
    "ACCELERATION": {"ACCELERATION", "UPTREND", "DISTRIBUTION"},
    "DISTRIBUTION": {"DISTRIBUTION", "ACCELERATION", "TOP_RISK", "DECLINE"},
    "TOP_RISK": {"TOP_RISK", "DISTRIBUTION", "DECLINE"},
    "DECLINE": {"DECLINE", "DEEP_BEAR", "BEAR"},
}

#: 最短持续期（天）——TOP_RISK 例外 3 天（风险预警需要更快响应）
MIN_PHASE_DURATION_DAYS = 7
TOP_RISK_MIN_DURATION_DAYS = 3

#: 转换确认天数——TOP_RISK/DECLINE 进入仅需 1 日（风险方向宁快勿慢）
CONFIRMATION_DAYS = 3
FAST_CONFIRM_PHASES = {"TOP_RISK", "DECLINE"}

#: 各阶段概率分布的中心得分（softmax 打分用）
_PHASE_SCORE_CENTERS: dict[str, float] = {
    "DEEP_BEAR": -0.85,
    "BEAR": -0.55,
    "BOTTOM_BUILDING": -0.28,
    "RECOVERY": 0.0,
    "UPTREND": 0.30,
    "ACCELERATION": 0.60,
    "DISTRIBUTION": 0.72,
    "TOP_RISK": 0.82,
    "DECLINE": -0.62,
}

#: 阶段中文名与普通用户解释模板
PHASE_LABELS: dict[str, tuple[str, str]] = {
    "DEEP_BEAR": ("深度熊市", "市场处于深度熊市：价格远低于长期均线，链上估值和情绪都在历史低位，恐慌已大幅释放"),
    "BEAR": ("熊市", "市场处于熊市：下跌趋势明确，反弹持续被卖压压制，资金仍在流出"),
    "BOTTOM_BUILDING": ("底部构筑", "市场可能在构筑底部：跌势放缓、横盘震荡，长期持有者开始吸筹，但情绪仍然偏冷"),
    "RECOVERY": ("恢复期", "市场处于恢复期：价格站回长期均线附近，估值脱离极低区域，资金流开始转正"),
    "UPTREND": ("趋势上涨", "市场处于趋势上涨阶段：均线多头排列，增量资金持续流入，链上行为健康"),
    "ACCELERATION": ("加速上涨", "市场处于加速上涨阶段：价格快速远离长期均线，散户热度和杠杆水平明显抬升"),
    "DISTRIBUTION": ("高位分配", "市场出现高位分配迹象：价格滞涨震荡，长期持有者开始派发筹码，情绪偏热"),
    "TOP_RISK": ("顶部风险", "市场出现顶部风险信号：估值、杠杆、情绪多项指标同时进入极端区域，需警惕反转"),
    "DECLINE": ("下跌期", "市场进入下跌期：趋势反转得到确认，资金净流出，清算压力放大"),
}


def score_to_phase(score: float) -> str:
    """综合得分（-1~+1）→ 基础阶段（未经规则修正）。"""
    for threshold, phase in PHASE_THRESHOLDS:
        if score < threshold:
            return phase
    return CyclePhase.ACCELERATION.value


# --------------------------------------------------------------------------- #
# CycleEngine
# --------------------------------------------------------------------------- #

class CycleEngine(EngineBase):
    """市场周期引擎。

    Usage::

        engine = CycleEngine(
            market_service=..., onchain_service=..., etf_service=...,
            derivatives_service=..., macro_service=..., sentiment_service=...,
        )
        output = await engine.calculate()          # 实时
        output = await engine.calculate(as_of=t)   # 历史重算（Point-in-Time）
        await engine.save_result(output)
    """

    #: Top1/Top2 概率差小于该值时输出「过渡期」标记并封顶置信度（§1.3）
    TRANSITION_GAP = 0.10
    #: 过渡期置信度封顶
    TRANSITION_CONFIDENCE_CAP = 0.6
    #: 历史模式匹配先验修正权重（§1.3 第三层，20%）
    PATTERN_PRIOR_WEIGHT = 0.20

    def __init__(
        self,
        *,
        market_service: Any = None,
        onchain_service: Any = None,
        etf_service: Any = None,
        derivatives_service: Any = None,
        macro_service: Any = None,
        sentiment_service: Any = None,
        valuation_engine: Any = None,
        pattern_matcher: PatternMatcher | None = None,
        factor_weights: dict[str, float] | None = None,
        persist: bool = True,
    ) -> None:
        """初始化 CycleEngine。

        Args:
            market_service: 行情服务（必需，价格趋势因子）
            onchain_service: 链上服务（链上/资金流因子）
            etf_service: ETF 服务（资金流因子）
            derivatives_service: 衍生品服务
            macro_service: 宏观服务
            sentiment_service: 情绪服务
            valuation_engine: 估值引擎实例（估值锚因子；None 时该因子降级）
            pattern_matcher: 历史模式匹配器（None 时使用默认配置）
            factor_weights: 自定义因子权重（None 时使用 CYCLE_FACTOR_WEIGHTS）
            persist: calculate() 后是否自动持久化（默认 True）
        """
        self._market = market_service
        self._onchain = onchain_service
        self._etf = etf_service
        self._derivatives = derivatives_service
        self._macro = macro_service
        self._sentiment = sentiment_service
        self._valuation_engine = valuation_engine
        self._matcher = pattern_matcher or PatternMatcher()
        self._weights = dict(factor_weights or CYCLE_FACTOR_WEIGHTS)
        self._persist = persist

    @property
    def name(self) -> str:
        return "cycle"

    @property
    def _orm_model(self) -> type:
        return CycleState

    # ---- 主计算 ----

    async def calculate(self, as_of: datetime | None = None) -> EngineOutput:
        """计算当前（或指定时刻）的市场周期阶段。"""
        obs = self._to_utc(as_of)
        logger.info(f"[cycle] 开始计算周期阶段 as_of={obs.isoformat()}")

        # 1. 并发计算全部因子
        scores, evidence_list, data_gaps = await self._compute_factors(obs)

        # 2. 加权融合（按置信度×质量衰减加权，自动跳过降级因子）
        composite = self._fuse_scores(scores, evidence_list)

        # 3. 第一层：得分 → 阶段概率分布（softmax）
        probabilities = self._phase_probabilities(composite)

        # 4. 第二层：规则引擎（VETO/PROMOTE）
        phase, rule_evidence, rule_notes = self._apply_rules(
            score_to_phase(composite), scores, probabilities
        )
        evidence_list.extend(rule_evidence)

        # 5. 第三层：历史模式匹配 + 先验修正
        similar_dicts, prior_applied = await self._match_patterns(scores, obs)
        if prior_applied:
            phase = self._apply_prior(phase, probabilities, prior_applied)

        # 6. 过渡期检测（Top1/Top2 概率差 < 10pp → 置信度封顶）
        sorted_probs = sorted(probabilities.values(), reverse=True)
        in_transition = (
            len(sorted_probs) >= 2
            and (sorted_probs[0] - sorted_probs[1]) < self.TRANSITION_GAP
        )

        # 7. 防抖状态机校验
        phase, prev_phase, duration_days, transition_note, pending = (
            await self._debounce(phase, obs)
        )
        if transition_note:
            rule_notes.append(transition_note)

        # 8. 构建输出
        explanation = self._build_explanation(phase, composite, in_transition)
        if rule_notes:
            explanation = f"{explanation}。{'；'.join(rule_notes[:2])}"

        dimension_scores = {k: round(v, 4) for k, v in scores.items()}
        output = self._build_output(
            state=phase,
            score=composite,
            evidence=evidence_list,
            explanation=explanation,
            observation_time=obs,
            data_quality=self._overall_quality(evidence_list),
            dimension_scores=dimension_scores,
            historical_similar=similar_dicts,
            data_gaps=data_gaps,
            metadata={
                "phase_probabilities": {k: round(v, 4) for k, v in probabilities.items()},
                "previous_phase": prev_phase,
                "phase_duration_days": duration_days,
                "in_transition": in_transition,
                "pending_phase": pending.get("phase"),
                "pending_count": pending.get("count", 0),
                "rule_notes": rule_notes,
            },
        )

        # 过渡期置信度封顶（诚实表达不确定性）
        if in_transition:
            top2 = sorted(probabilities.items(), key=lambda kv: kv[1], reverse=True)[:2]
            output.confidence = min(output.confidence, self.TRANSITION_CONFIDENCE_CAP)
            output.metadata["transition_target"] = top2[1][0] if len(top2) > 1 else None
            logger.info(
                f"[cycle] 阶段过渡期: {top2[0][0]}({top2[0][1]:.0%}) vs "
                f"{top2[1][0]}({top2[1][1]:.0%})，置信度封顶 {self.TRANSITION_CONFIDENCE_CAP}"
            )

        logger.info(
            f"[cycle] 计算完成: phase={phase} score={composite:+.3f} "
            f"confidence={output.confidence:.2f} gaps={len(data_gaps)}"
        )

        if self._persist:
            await self.save_result(output)
            if prev_phase and prev_phase != phase:
                await self._emit_change_signal(output, prev_phase)
        return output

    async def calculate_historical(
        self, start: datetime, end: datetime
    ) -> list[EngineOutput]:
        """按日计算历史阶段序列（逐日推进，保证状态机时序正确）。"""
        outputs: list[EngineOutput] = []
        day = self._to_utc(start)
        end_utc = self._to_utc(end)
        while day <= end_utc:
            try:
                out = await self.calculate(as_of=day)
                outputs.append(out)
            except Exception as exc:
                logger.error(f"[cycle] 历史计算失败 {day.date()}: {exc!r}")
            day += timedelta(days=1)
        logger.info(f"[cycle] 历史计算完成: {len(outputs)} 天 ({start.date()} ~ {end.date()})")
        return outputs

    # ---- 因子计算 ----

    async def _compute_factors(
        self, obs: datetime
    ) -> tuple[dict[str, float], list[EngineEvidence], list[str]]:
        """并发计算 7 个因子，返回 (分数, 证据, 数据缺口)。"""
        valuation_output = None
        if self._valuation_engine is not None:
            try:
                valuation_output = await self._valuation_engine.calculate(as_of=obs)
            except Exception as exc:
                logger.warning(f"[cycle] 估值引擎调用失败（估值锚因子降级）: {exc!r}")

        tasks = {
            "price_trend": self._guard(
                calculate_price_trend_factor(self._market, obs), "价格趋势"
            ) if self._market else None,
            "onchain": self._guard(
                calculate_onchain_factor(self._onchain, obs), "链上"
            ) if self._onchain else None,
            "capital_flow": self._guard(
                calculate_capital_flow_factor(self._onchain, self._etf, obs), "资金流"
            ) if (self._onchain or self._etf) else None,
            "derivative": self._guard(
                calculate_derivative_factor(self._derivatives, obs), "衍生品"
            ) if self._derivatives else None,
            "macro": self._guard(
                calculate_macro_factor(self._macro, obs), "宏观"
            ) if self._macro else None,
            "sentiment": self._guard(
                calculate_sentiment_factor(self._sentiment, obs), "情绪"
            ) if self._sentiment else None,
            "valuation_anchor": calculate_valuation_anchor_factor(valuation_output),
        }

        scores: dict[str, float] = {}
        evidence_list: list[EngineEvidence] = []
        data_gaps: list[str] = []

        active = {k: t for k, t in tasks.items() if t is not None}
        results = await asyncio.gather(*active.values(), return_exceptions=True)
        for key, res in zip(active.keys(), results):
            if isinstance(res, BaseException):
                logger.warning(f"[cycle] 因子 {key} 计算异常: {res!r}")
                scores[key] = 0.0
                evidence_list.append(self._missing_evidence(key, str(res)))
                data_gaps.append(key)
                continue
            score, ev = res
            # 应用配置权重（覆盖因子内默认权重）
            ev.weight = self._weights.get(key, ev.weight)
            scores[key] = clamp(score)
            evidence_list.append(ev)
            if ev.confidence <= 0:
                data_gaps.append(key)

        # 未注入对应 Service 的因子直接标记缺口
        for key in tasks:
            if tasks[key] is None:
                scores[key] = 0.0
                data_gaps.append(key)
                evidence_list.append(self._missing_evidence(key, "数据服务未配置"))

        return scores, evidence_list, data_gaps

    @staticmethod
    async def _guard(coro: Any, label: str) -> tuple[float, EngineEvidence]:
        """因子协程守护（异常时返回降级证据）。"""
        try:
            return await coro
        except Exception as exc:
            logger.warning(f"[cycle] {label} 因子异常: {exc!r}")
            return 0.0, CycleEngine._missing_evidence(label, str(exc))

    @staticmethod
    def _missing_evidence(factor: str, reason: str) -> EngineEvidence:
        """构造缺失因子的降级证据。"""
        return EngineEvidence(
            factor=factor,
            value=None,
            interpretation=f"{factor}因子数据暂不可用（{reason}），不参与本次判断",
            weight=0.0,
            supports="neutral",
            confidence=0.0,
            data_source="cycle_engine",
            quality_status="INVALID",
        )

    def _fuse_scores(
        self, scores: dict[str, float], evidence_list: list[EngineEvidence]
    ) -> float:
        """加权融合：有效权重 = 配置权重 × 证据置信度 × 质量系数。"""
        ev_by_factor: dict[str, EngineEvidence] = {}
        for ev in evidence_list:
            ev_by_factor.setdefault(ev.factor, ev)

        # 证据的 factor 字段是中文名，这里直接按 scores 键找对应证据置信度
        label_map = {
            "price_trend": "价格趋势", "onchain": "链上", "capital_flow": "资金流",
            "derivative": "衍生品", "macro": "宏观", "sentiment": "情绪",
            "valuation_anchor": "估值",
        }
        num = 0.0
        den = 0.0
        for key, score in scores.items():
            base_w = self._weights.get(key, 0.0)
            ev = ev_by_factor.get(label_map.get(key, key))
            conf = ev.confidence if ev else 0.0
            qual = quality_coefficient(ev.quality_status) if ev else 0.0
            eff_w = base_w * conf * qual
            if eff_w <= 0:
                continue
            num += score * eff_w
            den += eff_w
        composite = num / den if den > 0 else 0.0
        logger.debug(f"[cycle] 因子融合: composite={composite:+.4f} (有效权重和={den:.3f})")
        return clamp(composite)

    # ---- 第一层：阶段概率分布 ----

    def _phase_probabilities(self, composite: float) -> dict[str, float]:
        """综合得分 → 9 阶段 softmax 概率分布。

        DISTRIBUTION/TOP_RISK/DECLINE 不由得分直接映射（它们是「状态」而非
        「水平」），此处赋予基础小概率，由规则引擎负责提升。
        """
        sigma = 0.30
        raw: dict[str, float] = {}
        for phase, center in _PHASE_SCORE_CENTERS.items():
            raw[phase] = math.exp(-((composite - center) ** 2) / (2 * sigma * sigma))
        # 状态型阶段的基础衰减（除非规则提升）
        for p in ("DISTRIBUTION", "TOP_RISK", "DECLINE"):
            raw[p] *= 0.15
        total = sum(raw.values()) or 1.0
        return {k: v / total for k, v in raw.items()}

    # ---- 第二层：规则引擎 ----

    def _apply_rules(
        self,
        base_phase: str,
        scores: dict[str, float],
        probabilities: dict[str, float],
    ) -> tuple[str, list[EngineEvidence], list[str]]:
        """守卫规则（VETO/PROMOTE）。触发时返回修正后阶段 + 规则证据。

        红线：规则中禁止出现「距减半 N 天」类时间硬编码。
        """
        evidence: list[EngineEvidence] = []
        notes: list[str] = []
        phase = base_phase

        valuation_anchor = scores.get("valuation_anchor", 0.0)
        derivative = scores.get("derivative", 0.0)
        price_trend = scores.get("price_trend", 0.0)
        sentiment = scores.get("sentiment", 0.0)

        # PROMOTE: 估值极端（锚分 ≤ -0.6）+ 杠杆过热（≥ 0.4）→ TOP_RISK
        if valuation_anchor <= -0.6 and derivative >= 0.4:
            phase = CyclePhase.TOP_RISK.value
            notes.append("估值处于极端高位且杠杆过热，触发顶部风险规则")
            evidence.append(EngineEvidence(
                factor="规则:顶部风险触发",
                value={"valuation_anchor": round(valuation_anchor, 3),
                       "derivative": round(derivative, 3)},
                interpretation="估值极端偏高 + 衍生品杠杆过热同时出现，历史上这类组合多发生在顶部区域",
                weight=0.0, supports="bearish", confidence=0.85,
                data_source="rule_engine", indicator_code="rule.top_risk",
            ))

        # PROMOTE: 估值偏高（锚分 ≤ -0.35）+ 价格趋势减速（< 0.1）+ 情绪过热（≤ -0.3，F&G 反向）→ DISTRIBUTION
        elif (
            valuation_anchor <= -0.35
            and price_trend < 0.1
            and sentiment <= -0.3
            and phase in (CyclePhase.UPTREND.value, CyclePhase.ACCELERATION.value)
        ):
            phase = CyclePhase.DISTRIBUTION.value
            notes.append("价格滞涨但估值偏热、情绪极度乐观，触发高位分配规则")
            evidence.append(EngineEvidence(
                factor="规则:高位分配触发",
                value={"valuation_anchor": round(valuation_anchor, 3),
                       "price_trend": round(price_trend, 3),
                       "sentiment": round(sentiment, 3)},
                interpretation="价格停止上涨但市场情绪仍然极度乐观、估值偏高，符合高位派发特征",
                weight=0.0, supports="bearish", confidence=0.75,
                data_source="rule_engine", indicator_code="rule.distribution",
            ))

        # PROMOTE: 趋势快速恶化（≤ -0.5）且此前处于上涨侧 → DECLINE
        if (
            price_trend <= -0.5
            and probabilities.get(CyclePhase.DECLINE.value, 0) > 0.05
            and phase in (
                CyclePhase.DISTRIBUTION.value, CyclePhase.TOP_RISK.value,
                CyclePhase.ACCELERATION.value, CyclePhase.UPTREND.value,
            )
        ):
            phase = CyclePhase.DECLINE.value
            notes.append("价格趋势快速恶化，触发下跌确认规则")
            evidence.append(EngineEvidence(
                factor="规则:下跌确认触发",
                value={"price_trend": round(price_trend, 3)},
                interpretation="价格已明显跌破关键均线且下行加速，趋势反转得到确认",
                weight=0.0, supports="bearish", confidence=0.8,
                data_source="rule_engine", indicator_code="rule.decline",
            ))

        # VETO: 价格趋势深度为负（≤ -0.45）时禁止输出上涨侧阶段
        bullish_phases = {
            CyclePhase.UPTREND.value, CyclePhase.ACCELERATION.value,
            CyclePhase.DISTRIBUTION.value, CyclePhase.TOP_RISK.value,
        }
        if price_trend <= -0.45 and phase in bullish_phases:
            phase = CyclePhase.BEAR.value
            notes.append("价格深度低于长期均线，否决上涨侧阶段判定")
            evidence.append(EngineEvidence(
                factor="规则:上涨侧否决",
                value={"price_trend": round(price_trend, 3)},
                interpretation="价格深度跌破长期均线时，系统不允许判定为上涨或顶部阶段（守卫规则）",
                weight=0.0, supports="bearish", confidence=0.9,
                data_source="rule_engine", indicator_code="rule.veto_bullish",
            ))

        return phase, evidence, notes

    # ---- 第三层：历史模式匹配 ----

    async def _match_patterns(
        self, scores: dict[str, float], obs: datetime
    ) -> tuple[list[dict[str, Any]] | None, dict[str, float]]:
        """历史相似检索，返回 (相似时段字典列表, 先验概率分布)。"""
        try:
            similar: list[SimilarPeriod] = await self._matcher.find_similar_periods(
                current_factors=scores, top_n=3, as_of=obs
            )
        except Exception as exc:
            logger.warning(f"[cycle] 历史模式匹配失败（该层降级）: {exc!r}")
            return None, {}
        if not similar:
            return None, {}
        prior = await self._matcher.get_phase_prior(similar)
        return [p.to_dict() for p in similar], prior

    def _apply_prior(
        self,
        phase: str,
        probabilities: dict[str, float],
        prior: dict[str, float],
    ) -> str:
        """先验修正：posterior = (1-w)×likelihood + w×prior（§1.3 第三层，w=20%）。"""
        if not prior:
            return phase
        w = self.PATTERN_PRIOR_WEIGHT
        posterior = {
            p: (1 - w) * probabilities.get(p, 0.0) + w * prior.get(p, 0.0)
            for p in probabilities
        }
        best = max(posterior, key=posterior.get)
        if best != phase:
            logger.info(f"[cycle] 历史先验修正: {phase} → {best} (prior={prior})")
        return best

    # ---- 防抖状态机 ----

    async def _debounce(
        self, target_phase: str, obs: datetime
    ) -> tuple[str, str | None, int, str | None, dict[str, Any]]:
        """状态机校验：返回 (最终阶段, 前阶段, 持续天数, 过渡说明, pending 信息)。

        规则（§1.5）：
        1. 无前态记录 → 直接采用目标阶段；
        2. 目标 == 前态 → 保持；
        3. 转换非法 → 保持前态，输出过渡期标记；
        4. 未达最短持续期 → 保持前态（TOP_RISK 例外 3 天）；
        5. 确认天数不足 → 保持前态，累计 pending；
        6. 全部通过 → 转换生效。
        """
        prev = await self._load_prev_state(obs)
        if prev is None:
            return target_phase, None, 0, None, {"phase": None, "count": 0}

        prev_phase = prev["phase"]
        prev_duration = prev.get("duration_days") or 0
        elapsed = max(0, (obs - prev["obs_time"]).days)
        duration_days = prev_duration + elapsed if prev_phase == target_phase else elapsed
        pending = prev.get("pending") or {"phase": None, "count": 0}

        if target_phase == prev_phase:
            return prev_phase, prev_phase, prev_duration + elapsed, None, {"phase": None, "count": 0}

        # 3. 合法性校验
        legal = LEGAL_TRANSITIONS.get(prev_phase, {prev_phase})
        if target_phase not in legal:
            note = (
                f"模型得分指向「{self._cn(target_phase)}」，但从「{self._cn(prev_phase)}」"
                f"直接跳转不符合阶段演进路径，市场结构变化快于阶段模型，判断置信度降低"
            )
            logger.info(f"[cycle] 非法转换被拦截: {prev_phase} → {target_phase}")
            return prev_phase, prev_phase, prev_duration + elapsed, note, {
                "phase": target_phase, "count": pending.get("count", 0),
            }

        # 4. 最短持续期
        min_days = (
            TOP_RISK_MIN_DURATION_DAYS
            if prev_phase == CyclePhase.TOP_RISK.value
            else MIN_PHASE_DURATION_DAYS
        )
        if prev_duration + 0 < min_days and elapsed < min_days and prev_duration < min_days:
            note = (
                f"当前阶段「{self._cn(prev_phase)}」持续 {prev_duration} 天，"
                f"未达最短持续期 {min_days} 天，暂不转换"
            )
            return prev_phase, prev_phase, prev_duration + elapsed, note, {
                "phase": target_phase, "count": pending.get("count", 0) + 1,
            }

        # 5. 确认天数（TOP_RISK/DECLINE 进入仅需 1 日）
        required = 1 if target_phase in FAST_CONFIRM_PHASES else CONFIRMATION_DAYS
        new_count = pending.get("count", 0) + 1 if pending.get("phase") == target_phase else 1
        if new_count < required:
            note = (
                f"「{self._cn(target_phase)}」信号出现 {new_count}/{required} 天，"
                f"等待连续确认后转换"
            )
            return prev_phase, prev_phase, prev_duration + elapsed, note, {
                "phase": target_phase, "count": new_count,
            }

        # 6. 转换生效
        logger.info(f"[cycle] 阶段转换生效: {prev_phase} → {target_phase}")
        return target_phase, prev_phase, 0, None, {"phase": None, "count": 0}

    async def _load_prev_state(self, obs: datetime) -> dict[str, Any] | None:
        """加载 obs 之前最近的周期状态记录（含 pending 元数据）。"""
        try:
            async with get_db_session_ctx() as session:
                stmt = (
                    select(CycleState)
                    .where(CycleState.observation_time < obs)
                    .order_by(CycleState.observation_time.desc())
                    .limit(1)
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is None:
                    return None
                phase = row.phase.value if hasattr(row.phase, "value") else str(row.phase)
                # pending 状态存储于 dimension_scores 的 _meta 键（JSONB 扩展位）
                meta = (row.dimension_scores or {}).get("_meta", {})
                return {
                    "phase": phase,
                    "obs_time": row.observation_time,
                    "duration_days": row.phase_duration_days or 0,
                    "pending": {
                        "phase": meta.get("pending_phase"),
                        "count": meta.get("pending_count", 0),
                    },
                }
        except Exception as exc:
            logger.warning(f"[cycle] 加载前态失败（跳过防抖）: {exc!r}")
            return None

    # ---- 持久化 ----

    def _build_row(self, output: EngineOutput, obs_time: datetime) -> dict[str, Any]:
        """EngineOutput → CycleState 行字典。"""
        supporting, opposing = output.evidence_dicts()
        # pending 状态写入 dimension_scores._meta（供下次防抖读取）
        dims = dict(output.dimension_scores)
        dims["_meta"] = {
            "pending_phase": output.metadata.get("pending_phase"),
            "pending_count": output.metadata.get("pending_count", 0),
            "composite_score": output.score,
        }
        prev = output.metadata.get("previous_phase")
        return {
            "observation_time": obs_time,
            "phase": CyclePhase(output.state),
            "previous_phase": CyclePhase(prev) if prev else None,
            "confidence": Decimal(str(round(output.confidence, 4))),
            "phase_duration_days": output.metadata.get("phase_duration_days"),
            "evidence_for": supporting,
            "evidence_against": opposing,
            "dimension_scores": dims,
            "historical_similar": output.historical_similar or [],
            "description": output.explanation or None,
            "quality_status": self._to_quality_enum(output.data_quality),
        }

    def _row_to_output(self, row: CycleState) -> EngineOutput:
        """CycleState 行 → EngineOutput（还原证据链）。"""
        dims = dict(row.dimension_scores or {})
        meta = dims.pop("_meta", {})
        supporting, opposing = self._restore_evidence(row.evidence_for, row.evidence_against)
        phase = row.phase.value if hasattr(row.phase, "value") else str(row.phase)
        return EngineOutput(
            state=phase,
            confidence=float(row.confidence) if row.confidence else 0.0,
            supporting_evidence=supporting,
            opposing_evidence=opposing,
            score=meta.get("composite_score", 0.0),
            historical_similar=row.historical_similar or None,
            observation_time=row.observation_time,
            data_quality=(
                row.quality_status.value
                if hasattr(row.quality_status, "value")
                else str(row.quality_status)
            ),
            explanation=row.description or "",
            dimension_scores=dims,
            metadata={
                "previous_phase": (
                    row.previous_phase.value
                    if hasattr(row.previous_phase, "value")
                    else row.previous_phase
                ),
                "phase_duration_days": row.phase_duration_days,
                "pending_phase": meta.get("pending_phase"),
                "pending_count": meta.get("pending_count", 0),
            },
        )

    @staticmethod
    def _restore_evidence(
        for_list: list | None, against_list: list | None
    ) -> tuple[list[EngineEvidence], list[EngineEvidence]]:
        """JSONB 证据字典 → EngineEvidence 列表。"""
        def _one(d: dict) -> EngineEvidence:
            return EngineEvidence(
                factor=d.get("factor", ""),
                value=d.get("value"),
                interpretation=d.get("interpretation", ""),
                weight=d.get("weight", 0.0),
                supports=d.get("supports", "neutral"),
                confidence=d.get("confidence", 0.0),
                data_source=d.get("data_source", ""),
                historical_percentile=d.get("historical_percentile"),
                indicator_code=d.get("indicator_code"),
                contribution=d.get("contribution"),
                quality_status=d.get("quality_status"),
            )
        return (
            [_one(d) for d in (for_list or []) if isinstance(d, dict)],
            [_one(d) for d in (against_list or []) if isinstance(d, dict)],
        )

    @staticmethod
    def _to_quality_enum(status: str) -> QualityStatus:
        """字符串 → QualityStatus 枚举（非法值回退 VERIFIED）。"""
        try:
            return QualityStatus(status)
        except ValueError:
            return QualityStatus.VERIFIED

    async def _emit_change_signal(self, output: EngineOutput, prev_phase: str) -> None:
        """阶段变化时写入 signals 表（type=CYCLE_CHANGE，§1.5）。"""
        try:
            from app.models.engine import Signal

            cn_new = self._cn(output.state)
            cn_prev = self._cn(prev_phase)
            direction = SignalDirection.NEUTRAL
            new_idx = self._phase_index(output.state)
            prev_idx = self._phase_index(prev_phase)
            if output.state in ("UPTREND", "ACCELERATION", "RECOVERY", "BOTTOM_BUILDING"):
                direction = SignalDirection.BULLISH
            elif output.state in ("DECLINE", "DEEP_BEAR", "TOP_RISK"):
                direction = SignalDirection.BEARISH
            supporting, opposing = output.evidence_dicts()
            async with get_db_session_ctx() as session:
                session.add(Signal(
                    signal_time=output.observation_time,
                    signal_type="CYCLE_CHANGE",
                    signal_direction=direction,
                    strength=Decimal(str(round(output.confidence, 4))),
                    category="CYCLE",
                    engine_source="CYCLE_ENGINE",
                    trigger_conditions=[{
                        "type": "phase_change",
                        "from": prev_phase, "to": output.state,
                        "composite_score": output.score,
                        "phase_index_shift": (new_idx - prev_idx) if new_idx is not None and prev_idx is not None else None,
                    }],
                    evidence=supporting[:5],
                    market_context={"confidence": output.confidence},
                    title=f"市场周期阶段变化: {cn_prev} → {cn_new}",
                    description=f"Cycle phase changed from {prev_phase} to {output.state}",
                    description_cn=output.explanation or f"周期阶段由「{cn_prev}」转为「{cn_new}」",
                    is_active=True,
                ))
            logger.info(f"[cycle] 阶段变化信号已写入: {prev_phase} → {output.state}")
        except Exception as exc:
            logger.warning(f"[cycle] 写入阶段变化信号失败: {exc!r}")

    # ---- 辅助 ----

    @staticmethod
    def _phase_index(phase: str) -> int | None:
        """阶段在演进序列中的位置（用于信号强度参考）。"""
        order = [
            "DEEP_BEAR", "BEAR", "BOTTOM_BUILDING", "RECOVERY",
            "UPTREND", "ACCELERATION", "DISTRIBUTION", "TOP_RISK", "DECLINE",
        ]
        return order.index(phase) if phase in order else None

    @staticmethod
    def _cn(phase: str) -> str:
        """阶段中文名。"""
        return PHASE_LABELS.get(phase, (phase, ""))[0]

    def _build_explanation(self, phase: str, composite: float, in_transition: bool) -> str:
        """构建普通用户可读的一句话解释。"""
        _, template = PHASE_LABELS.get(phase, (phase, "市场状态评估中"))
        suffix = "（当前处于阶段过渡期，判断存在不确定性）" if in_transition else ""
        return f"{template}{suffix}。综合周期得分 {composite:+.2f}（-1 极度看跌 ~ +1 极度看涨）"

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

_cycle_engine: CycleEngine | None = None


def get_cycle_engine() -> CycleEngine | None:
    """获取全局 CycleEngine 单例（未初始化时返回 None）。"""
    return _cycle_engine


def set_cycle_engine(engine: CycleEngine) -> None:
    """设置全局 CycleEngine 单例（应用启动时注入 Service 后调用）。"""
    global _cycle_engine
    _cycle_engine = engine
