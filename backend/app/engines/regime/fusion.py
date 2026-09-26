"""多因子融合（Market Regime Engine 核心，架构文档 §4.2）。

职责：
1. 将 9 个维度的原始分数标准化到 -1（极度看跌）~ +1（极度看涨）；
2. 加权融合：regime_score = Σ(w_d × v_d × q_d) / Σ(w_d × q_d)，q_d 为数据质量系数；
3. 状态枚举 → 标量映射表（版本化，集中管理）；
4. regime_score → 描述性 Regime 标签（6 类，任务规格）；
5. 定性修正：Risk=EXTREME 强制高风险环境；Cycle=TOP_RISK 且 score>0 → Euphoria；
6. 一致性/分歧度计算（confidence 基础 + 「市场分歧加大」标记）。

红线：只输出描述性标签，永不输出 BUY/SELL。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from app.engines.base import EngineEvidence, clamp, quality_coefficient

__all__ = [
    "REGIME_DIMENSION_WEIGHTS",
    "DimensionInput",
    "FusionResult",
    "RegimeFusion",
    "score_to_regime_label",
]


#: 9 维默认权重（架构文档 §4.1，Σ=1.0）
REGIME_DIMENSION_WEIGHTS: dict[str, float] = {
    "trend": 0.15,
    "valuation": 0.10,
    "capital_flow": 0.15,
    "onchain": 0.15,
    "derivative": 0.10,
    "macro": 0.10,
    "sentiment": 0.10,
    "risk": 0.10,
    "cycle": 0.05,
}

#: 状态枚举 → 标量映射表（§4.2 第 1 条，版本化管理）
STATE_SCORE_MAP: dict[str, dict[str, float]] = {
    "trend": {
        "STRONG_UP": 0.9, "UP": 0.5, "NEUTRAL": 0.0,
        "DOWN": -0.5, "STRONG_DOWN": -0.9,
    },
    "valuation": {
        # 估值映射到「价格上行空间」：低估=+，高估=−（仅作环境描述）
        "DEEP_UNDERVALUED": 0.9, "UNDERVALUED": 0.5, "FAIR": 0.0,
        "OVERVALUED": -0.5, "EXTREME_OVERVALUED": -0.9,
    },
    "capital_flow": {
        "STRONG_INFLOW": 0.9, "INFLOW": 0.4, "NEUTRAL": 0.0,
        "OUTFLOW": -0.4, "STRONG_OUTFLOW": -0.9,
    },
    "onchain": {
        "ACCUMULATION": 0.8, "NEUTRAL_ACC": 0.4, "NEUTRAL": 0.0,
        "NEUTRAL_DIST": -0.4, "DISTRIBUTION": -0.8,
    },
    "derivative": {
        "MILD_BULL": 0.4, "CALM": 0.0, "MILD_BEAR": -0.4,
        # OVERHEATED/STRESSED 是风险状态而非方向，映射见 _derivative_scalar
        "OVERHEATED": -0.2, "STRESSED": -0.7,
    },
    "macro": {
        "SUPPORTIVE": 0.7, "NEUTRAL": 0.0, "HEADWIND": -0.5, "CRISIS": -0.9,
    },
    "sentiment": {
        # 情绪是反向指标：极度恐惧往往是环境底部特征，极度贪婪是过热特征
        "EXTREME_FEAR": -0.9, "FEAR": -0.4, "NEUTRAL": 0.0,
        "GREED": 0.4, "EXTREME_GREED": 0.9,
    },
    "risk": {
        # 风险是反向映射：风险越高，环境越差
        "VERY_LOW": 0.6, "LOW": 0.3, "MODERATE": 0.0,
        "HIGH": -0.4, "VERY_HIGH": -0.7, "EXTREME": -0.9,
    },
    "cycle": {
        # 周期坐标（-1 周期底部 ~ +1 周期顶部）
        "DEEP_BEAR": -1.0, "BEAR": -0.7, "BOTTOM_BUILDING": -0.4,
        "RECOVERY": -0.1, "UPTREND": 0.4, "ACCELERATION": 0.7,
        "DISTRIBUTION": 0.85, "TOP_RISK": 1.0, "DECLINE": -0.5,
    },
}

#: regime_score → 标签阈值（任务规格 6 标签，降序匹配）
REGIME_LABELS: list[tuple[float, str, str, str]] = [
    # (下界, 标签, 中文名, 解读模板)
    (0.55, "Risk-On Expansion", "风险偏好扩张",
     "趋势、资金、情绪多数向好，市场处于风险偏好扩张阶段"),
    (0.20, "Cautious Optimism", "谨慎乐观",
     "市场偏强但动能一般，处于谨慎乐观阶段"),
    (-0.20, "Neutral", "中性",
     "多空力量均衡、方向不明，历史上此阶段假突破较多"),
    (-0.55, "Risk-Off Contraction", "风险规避收缩",
     "市场偏弱、反弹缺乏资金配合，处于风险规避收缩阶段"),
    (float("-inf"), "Capitulation", "投降性抛售",
     "趋势、资金、情绪全面转弱，市场可能处于投降性抛售阶段"),
]


def score_to_regime_label(score: float) -> tuple[str, str, str]:
    """regime_score → (英文标签, 中文名, 解读模板)。"""
    for lower, label, cn, template in REGIME_LABELS:
        if score >= lower:
            return label, cn, template
    return "Capitulation", "投降性抛售", REGIME_LABELS[-1][3]


@dataclass
class DimensionInput:
    """单维度融合输入。

    Attributes:
        key: 维度键（trend/valuation/...）
        value: 原始标量（-1~+1；None 表示该维度不可用）
        state: 维度状态枚举字符串（写入 market_regimes 各 *_state 列）
        quality_status: 数据质量状态（计算 q_d 衰减系数）
        confidence: 该维度自身置信度 0-1
        evidence: 维度证据链
    """

    key: str
    value: float | None = None
    state: str = "NEUTRAL"
    quality_status: str = "VERIFIED"
    confidence: float = 0.5
    evidence: list[EngineEvidence] = field(default_factory=list)


@dataclass
class FusionResult:
    """融合输出。"""

    regime_score: float
    label: str
    label_cn: str
    explanation: str
    confidence: float
    dimension_scalars: dict[str, float]
    dimension_states: dict[str, str]
    consistency: float
    divergence_flag: bool
    corrections: list[str]
    data_gaps: list[str]


class RegimeFusion:
    """九维融合器。

    Usage::

        fusion = RegimeFusion()
        result = fusion.fuse([
            DimensionInput("trend", 0.6, "UP"),
            DimensionInput("risk", -0.4, "HIGH"),
            ...
        ])
        print(result.label, result.regime_score)
    """

    #: 一致性低于该值 → 「市场分歧加大」标记
    DIVERGENCE_THRESHOLD = 0.55

    def __init__(self, weights: dict[str, float] | None = None) -> None:
        self.weights = dict(weights or REGIME_DIMENSION_WEIGHTS)

    # ---- 主融合 ----

    def fuse(
        self,
        dimensions: list[DimensionInput],
        *,
        risk_level: str | None = None,
        cycle_phase: str | None = None,
        valuation_level: str | None = None,
    ) -> FusionResult:
        """执行九维融合。

        Args:
            dimensions: 各维度输入（value=None 的维度自动跳过并声明缺口）
            risk_level: RiskEngine 输出的等级（定性修正用）
            cycle_phase: CycleEngine 输出的阶段（定性修正用）
            valuation_level: ValuationEngine 输出的等级（定性修正用）
        """
        scalars: dict[str, float] = {}
        states: dict[str, str] = {}
        weights_used: dict[str, float] = {}
        data_gaps: list[str] = []
        evidence_all: list[EngineEvidence] = []

        for dim in dimensions:
            states[dim.key] = dim.state
            for ev in dim.evidence:
                evidence_all.append(ev)
            if dim.value is None:
                data_gaps.append(dim.key)
                continue
            v = clamp(dim.value)
            q = quality_coefficient(dim.quality_status)
            w = self.weights.get(dim.key, 0.0) * max(dim.confidence, 0.1)
            if w * q <= 0:
                data_gaps.append(dim.key)
                continue
            scalars[dim.key] = v
            weights_used[dim.key] = w * q

        # regime_score = Σ(w×v×q) / Σ(w×q)
        total_w = sum(weights_used.values())
        if total_w > 0:
            regime_score = sum(
                scalars[k] * weights_used[k] for k in weights_used
            ) / total_w
        else:
            regime_score = 0.0
        regime_score = clamp(regime_score)

        # 一致性（同向维度权重占比）与分歧标记
        consistency = self._consistency(scalars, weights_used, regime_score)
        divergence = consistency < self.DIVERGENCE_THRESHOLD

        # 基础标签
        label, label_cn, template = score_to_regime_label(regime_score)

        # 定性修正（§4.2 第 3 条）
        corrections: list[str] = []
        if cycle_phase == "TOP_RISK" and regime_score > 0:
            label, label_cn = "Euphoria", "极度乐观（泡沫）"
            template = "价格动能仍强但周期引擎已识别顶部风险，市场处于极度乐观/泡沫特征区"
            corrections.append("Cycle=TOP_RISK 且综合分偏多 → 修正为 Euphoria（晚周期过热）")
        if risk_level == "EXTREME":
            template += "。注意：风险引擎判定当前为极端风险环境"
            corrections.append("Risk=EXTREME → 标签附加「高风险环境」")

        # 解释文案
        explanation = f"市场状态「{label_cn}」（{label}，综合分 {regime_score:+.2f}）：{template}"
        if divergence:
            explanation += "。各维度分歧较大（如趋势与资金/链上方向不一致），判断置信度已下调"
        if data_gaps:
            explanation += f"。{len(data_gaps)} 个维度数据缺失（{'、'.join(data_gaps)}）"

        # confidence = 一致性 × 覆盖率 × 质量
        coverage = total_w / max(
            sum(
                self.weights.get(d.key, 0.0) for d in dimensions
            ), 1e-9,
        )
        avg_q = (
            sum(quality_coefficient(d.quality_status) for d in dimensions if d.value is not None)
            / max(len([d for d in dimensions if d.value is not None]), 1)
        )
        confidence = clamp(0.9 * consistency * min(coverage, 1.0) * avg_q, 0.0, 1.0)

        logger.debug(
            f"[fusion] regime_score={regime_score:+.3f} label={label} "
            f"consistency={consistency:.2f} gaps={data_gaps}"
        )
        return FusionResult(
            regime_score=round(regime_score, 4),
            label=label,
            label_cn=label_cn,
            explanation=explanation,
            confidence=round(confidence, 4),
            dimension_scalars={k: round(v, 4) for k, v in scalars.items()},
            dimension_states=states,
            consistency=round(consistency, 4),
            divergence_flag=divergence,
            corrections=corrections,
            data_gaps=data_gaps,
        )

    # ---- 辅助 ----

    @staticmethod
    def _consistency(
        scalars: dict[str, float], weights: dict[str, float], score: float
    ) -> float:
        """方向一致性：与综合分同向（含中性）的维度权重占比 → [0.3, 1.0]。"""
        if not weights:
            return 0.3
        sign = 1.0 if score >= 0 else -1.0
        aligned = sum(
            w for k, w in weights.items()
            if scalars.get(k, 0.0) * sign >= -0.05
        )
        ratio = aligned / sum(weights.values())
        return 0.3 + 0.7 * ratio

    @staticmethod
    def state_to_scalar(dimension: str, state: str) -> float:
        """维度状态枚举 → 标量（未知状态回退 0）。"""
        return STATE_SCORE_MAP.get(dimension, {}).get(state.upper(), 0.0)

    @staticmethod
    def scalar_to_state(dimension: str, value: float) -> str:
        """标量 → 最近的维度状态枚举（用于写 *_state 列）。"""
        table = STATE_SCORE_MAP.get(dimension, {})
        if not table:
            return "NEUTRAL"
        return min(table.items(), key=lambda kv: abs(kv[1] - value))[0]
