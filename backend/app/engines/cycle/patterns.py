"""历史模式匹配（Cycle Engine 第三层）。

将当前因子特征向量与历史每日特征库做相似度检索（余弦 + 欧氏混合），
返回 Top-N 相似历史时段及其后续走势，供周期仲裁器做先验修正。

对应架构文档 §1.3 第三层。

设计要点
--------
- 特征向量：全部因子的标准化分数（-1~1），约 7~10 维；
- 相似度：余弦相似度 × 0.6 + 归一化欧氏距离 × 0.4（权重可配置）；
- 后续走势：相似时段之后 30/90/180 天的实际价格变化（仅事后视角）；
- 样本不足（< 3 个有效相似时段）时该层失效，confidence 相应下调。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from loguru import logger
from sqlalchemy import select

from app.core.database import get_db_session_ctx

__all__ = [
    "SimilarPeriod",
    "PatternMatcher",
]


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class SimilarPeriod:
    """历史相似时段。

    Attributes:
        start_date: 时段起始日期
        end_date: 时段结束日期
        similarity: 相似度 0-1
        factor_vector: 当时的因子向量
        phase_at_time: 当时的周期阶段（如有历史记录）
        subsequent_30d: 后续 30 天价格变化率
        subsequent_90d: 后续 90 天价格变化率
        subsequent_180d: 后续 180 天价格变化率
        btc_price_at_time: 当时 BTC 价格
        description: 描述性文案
    """

    start_date: datetime
    end_date: datetime
    similarity: float = 0.0
    factor_vector: dict[str, float] = field(default_factory=dict)
    phase_at_time: str | None = None
    subsequent_30d: float | None = None
    subsequent_90d: float | None = None
    subsequent_180d: float | None = None
    btc_price_at_time: float | None = None
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化字典。"""
        return {
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "similarity": round(self.similarity, 4),
            "factor_vector": {k: round(v, 4) for k, v in self.factor_vector.items()},
            "phase_at_time": self.phase_at_time,
            "subsequent_30d": round(self.subsequent_30d, 4) if self.subsequent_30d is not None else None,
            "subsequent_90d": round(self.subsequent_90d, 4) if self.subsequent_90d is not None else None,
            "subsequent_180d": round(self.subsequent_180d, 4) if self.subsequent_180d is not None else None,
            "btc_price_at_time": round(self.btc_price_at_time, 2) if self.btc_price_at_time else None,
            "description": self.description,
        }


# --------------------------------------------------------------------------- #
# 相似度计算
# --------------------------------------------------------------------------- #

def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度（-1~1，越大越相似）。"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _euclidean_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """欧氏距离。"""
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def _normalized_euclidean_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """归一化欧氏相似度（0~1，越大越相似）。

    最大可能距离 = sqrt(2 * N)（每个维度差 2，从 -1 到 +1）。
    """
    n = len(a)
    if n == 0:
        return 0.0
    max_dist = math.sqrt(2.0 * n)
    dist = _euclidean_distance(a, b)
    return max(0.0, 1.0 - dist / max_dist)


def combined_similarity(
    a: Sequence[float],
    b: Sequence[float],
    cosine_weight: float = 0.6,
) -> float:
    """混合相似度 = 余弦 × w + 归一化欧氏 × (1-w)。"""
    cos = _cosine_similarity(a, b)
    # 余弦从 [-1,1] 映射到 [0,1]
    cos_norm = (cos + 1.0) / 2.0
    euc = _normalized_euclidean_similarity(a, b)
    return cosine_weight * cos_norm + (1.0 - cosine_weight) * euc


# --------------------------------------------------------------------------- #
# PatternMatcher
# --------------------------------------------------------------------------- #

class PatternMatcher:
    """历史模式匹配器。

    从数据库加载历史因子特征向量（cycle_states.dimension_scores），
    与当前特征向量做相似度检索，返回 Top-N 相似时段。

    当历史数据不足时（< min_samples 条记录），返回空列表并标记失效。

    Usage::

        matcher = PatternMatcher()
        similar = await matcher.find_similar_periods(
            current_factors={"price_trend": 0.6, "onchain": 0.3, ...},
            top_n=3,
        )
    """

    #: 最低有效样本数（低于此值该层失效）
    MIN_SAMPLES = 30
    #: 相似度阈值（低于此值不纳入结果）
    SIMILARITY_THRESHOLD = 0.55
    #: 余弦权重
    COSINE_WEIGHT = 0.6

    def __init__(
        self,
        *,
        min_samples: int = MIN_SAMPLES,
        similarity_threshold: float = SIMILARITY_THRESHOLD,
        cosine_weight: float = COSINE_WEIGHT,
    ) -> None:
        self.min_samples = min_samples
        self.similarity_threshold = similarity_threshold
        self.cosine_weight = cosine_weight

    async def find_similar_periods(
        self,
        current_factors: dict[str, float],
        *,
        top_n: int = 3,
        as_of: datetime | None = None,
        lookback_days: int = 1800,
    ) -> list[SimilarPeriod]:
        """在历史中找到与当前因子组合最相似的时期。

        Args:
            current_factors: 当前因子分数字典 {factor_name: score(-1~1)}
            top_n: 返回前 N 个相似时段
            as_of: 当前观测时间（排除此时间之后的数据）
            lookback_days: 向前回溯天数（默认 ~5 年）

        Returns:
            按相似度降序的 SimilarPeriod 列表；样本不足时返回空列表。
        """
        if not current_factors:
            logger.warning("[pattern] 当前因子为空，跳过历史模式匹配")
            return []

        as_of = as_of or datetime.now(timezone.utc)
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
        start = as_of - timedelta(days=lookback_days)

        # 加载历史特征向量
        historical = await self._load_historical_features(start, as_of)
        if len(historical) < self.min_samples:
            logger.info(
                f"[pattern] 历史样本不足（{len(historical)} < {self.min_samples}），"
                f"历史模式匹配层失效"
            )
            return []

        # 构建当前特征向量
        factor_names = sorted(current_factors.keys())
        current_vec = [current_factors.get(f, 0.0) for f in factor_names]

        # 计算相似度并排序
        scored: list[tuple[float, dict[str, Any]]] = []
        for record in historical:
            hist_factors = record.get("dimension_scores", {})
            if not hist_factors:
                continue
            hist_vec = [hist_factors.get(f, 0.0) for f in factor_names]
            sim = combined_similarity(current_vec, hist_vec, self.cosine_weight)
            if sim >= self.similarity_threshold:
                scored.append((sim, record))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:top_n]

        if not top:
            logger.info("[pattern] 未找到超过阈值的相似时段")
            return []

        # 构建结果（附带后续走势）
        results: list[SimilarPeriod] = []
        for sim, record in top:
            obs_time = record.get("observation_time")
            if obs_time is None:
                continue
            period = SimilarPeriod(
                start_date=obs_time - timedelta(days=7),
                end_date=obs_time + timedelta(days=7),
                similarity=sim,
                factor_vector=record.get("dimension_scores", {}),
                phase_at_time=record.get("phase"),
                btc_price_at_time=record.get("btc_price"),
            )
            # 后续走势（从 K 线数据计算）
            await self._fill_subsequent_returns(period, obs_time)
            period.description = self._build_description(period)
            results.append(period)

        logger.info(
            f"[pattern] 找到 {len(results)} 个相似时段 "
            f"(top similarity={results[0].similarity:.3f})" if results else "[pattern] 无相似时段"
        )
        return results

    async def _load_historical_features(
        self, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """从 cycle_states 表加载历史特征向量。"""
        try:
            from app.models.engine import CycleState

            async with get_db_session_ctx() as session:
                stmt = (
                    select(
                        CycleState.observation_time,
                        CycleState.phase,
                        CycleState.dimension_scores,
                        CycleState.confidence,
                    )
                    .where(
                        CycleState.observation_time >= start,
                        CycleState.observation_time <= end,
                        CycleState.dimension_scores.isnot(None),
                    )
                    .order_by(CycleState.observation_time)
                )
                rows = (await session.execute(stmt)).all()
                return [
                    {
                        "observation_time": r[0],
                        "phase": r[1].value if hasattr(r[1], "value") else str(r[1]),
                        "dimension_scores": r[2] or {},
                        "confidence": float(r[3]) if r[3] else 0.0,
                    }
                    for r in rows
                    if r[2]  # 跳过空 dimension_scores
                ]
        except Exception as exc:
            logger.warning(f"[pattern] 加载历史特征失败: {exc!r}")
            return []

    async def _fill_subsequent_returns(
        self, period: SimilarPeriod, base_time: datetime
    ) -> None:
        """计算相似时段后续 30/90/180 天的价格变化率。"""
        try:
            from app.models.market import Candle
            from app.models.enums import CandleInterval

            async with get_db_session_ctx() as session:
                # 获取基准价格
                base_stmt = (
                    select(Candle.close)
                    .where(
                        Candle.symbol == "BTCUSDT",
                        Candle.interval == CandleInterval.D1,
                        Candle.observation_time <= base_time,
                    )
                    .order_by(Candle.observation_time.desc())
                    .limit(1)
                )
                base_price = (await session.execute(base_stmt)).scalar_one_or_none()
                if base_price is None:
                    return
                period.btc_price_at_time = float(base_price)

                # 后续价格
                for days, attr in [(30, "subsequent_30d"), (90, "subsequent_90d"), (180, "subsequent_180d")]:
                    target_time = base_time + timedelta(days=days)
                    fut_stmt = (
                        select(Candle.close)
                        .where(
                            Candle.symbol == "BTCUSDT",
                            Candle.interval == CandleInterval.D1,
                            Candle.observation_time <= target_time,
                        )
                        .order_by(Candle.observation_time.desc())
                        .limit(1)
                    )
                    fut_price = (await session.execute(fut_stmt)).scalar_one_or_none()
                    if fut_price is not None and float(base_price) > 0:
                        setattr(
                            period, attr,
                            (float(fut_price) - float(base_price)) / float(base_price),
                        )
        except Exception as exc:
            logger.debug(f"[pattern] 计算后续走势失败: {exc!r}")

    @staticmethod
    def _build_description(period: SimilarPeriod) -> str:
        """构建相似时段的描述性文案。"""
        parts: list[str] = []
        date_str = period.start_date.strftime("%Y-%m")
        parts.append(f"{date_str} 前后")

        if period.phase_at_time:
            phase_cn = {
                "DEEP_BEAR": "深度熊市", "BEAR": "熊市",
                "BOTTOM_BUILDING": "底部构筑", "RECOVERY": "恢复期",
                "UPTREND": "趋势上涨", "ACCELERATION": "加速上涨",
                "DISTRIBUTION": "高位分配", "TOP_RISK": "顶部风险",
                "DECLINE": "下跌期",
            }.get(period.phase_at_time, period.phase_at_time)
            parts.append(f"系统判定为「{phase_cn}」")

        if period.subsequent_90d is not None:
            direction = "上涨" if period.subsequent_90d > 0 else "下跌"
            parts.append(f"随后 90 天{direction} {abs(period.subsequent_90d):.0%}")

        parts.append(f"（相似度 {period.similarity:.0%}）")
        return "，".join(parts)

    async def get_phase_prior(
        self,
        similar_periods: list[SimilarPeriod],
    ) -> dict[str, float]:
        """从相似时段的后续阶段分布中提取先验概率。

        用于周期仲裁器的先验修正（架构文档 §1.3 第三层，权重 20%）。

        Returns:
            {phase: probability} 字典；样本不足时返回空字典。
        """
        if len(similar_periods) < 3:
            return {}

        # 统计相似时段后续（90 天）的阶段分布
        # 简化实现：根据后续收益方向推断阶段
        phase_counts: dict[str, int] = {}
        for p in similar_periods:
            ret_90 = p.subsequent_90d
            if ret_90 is None:
                continue
            if ret_90 > 0.3:
                phase = "UPTREND"
            elif ret_90 > 0.05:
                phase = "RECOVERY"
            elif ret_90 > -0.05:
                phase = "BOTTOM_BUILDING"
            elif ret_90 > -0.3:
                phase = "BEAR"
            else:
                phase = "DEEP_BEAR"
            phase_counts[phase] = phase_counts.get(phase, 0) + 1

        total = sum(phase_counts.values())
        if total == 0:
            return {}
        return {k: v / total for k, v in phase_counts.items()}
