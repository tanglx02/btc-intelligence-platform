"""引擎公共基础设施。

定义全部业务引擎的统一契约（对应架构文档 §0.2 / §0.3 / §8.1）：

- :class:`EngineEvidence` — 单条证据（因子名、当前值、解释、权重、方向、置信度、来源）
- :class:`EngineOutput`   — 引擎统一输出（状态 + 置信度 + 证据链 + 历史相似 + 解释）
- :class:`EngineBase`      — 抽象基类（calculate / calculate_historical / save / query）

设计原则
--------
1. **描述性输出，禁止指令性输出**：永不输出 BUY/SELL；
2. **一切结论可解释**：每条输出携带完整证据链（支持 + 反向）；
3. **数据置信度传导**：输入降级 → 权重衰减 → 置信度下降；
4. **优雅降级**：任一数据源不可用时跳过该因子、降低置信度，不中断计算。
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional, Sequence

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.database import get_db_session_ctx

__all__ = [
    "EngineEvidence",
    "EngineOutput",
    "EngineBase",
    "clamp",
    "safe_div",
    "weighted_average",
    "percentile_rank",
    "normalize_weights",
    "quality_coefficient",
]


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #

def clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    """将数值限制在 [lo, hi] 区间。"""
    if value != value:  # NaN
        return 0.0
    return max(lo, min(hi, value))


def safe_div(a: float | None, b: float | None, default: float = 0.0) -> float:
    """安全除法：None / 0 / NaN 均返回 default。"""
    if a is None or b is None:
        return default
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return default
    if fb == 0 or fa != fa or fb != fb:
        return default
    return fa / fb


def weighted_average(
    scores: Sequence[float], weights: Sequence[float]
) -> float:
    """加权平均（自动跳过 None/NaN，权重归一化）。"""
    total_w = 0.0
    total_s = 0.0
    for s, w in zip(scores, weights):
        if s is None or w is None:
            continue
        try:
            fs, fw = float(s), float(w)
        except (TypeError, ValueError):
            continue
        if fs != fs or fw != fw or fw <= 0:
            continue
        total_s += fs * fw
        total_w += fw
    if total_w == 0:
        return 0.0
    return total_s / total_w


def percentile_rank(values: Sequence[float], target: float) -> float | None:
    """计算 target 在 values 中的百分位（0-100）。

    使用「严格小于」计数法：PctRank = count(x < target) / N × 100。
    values 为空或全 NaN 时返回 None。
    """
    clean = [v for v in values if v is not None and v == v]
    if not clean:
        return None
    n = len(clean)
    below = sum(1 for v in clean if v < target)
    return below / n * 100.0


def normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    """归一化权重字典（Σ=1）；全零时等权分配。"""
    total = sum(max(0.0, w) for w in weights.values())
    if total <= 0:
        n = len(weights) or 1
        return {k: 1.0 / n for k in weights}
    return {k: max(0.0, w) / total for k, w in weights.items()}


#: 数据质量 → 权重衰减系数（架构文档 §0.3）
_QUALITY_COEFF = {
    "VERIFIED": 1.0,
    "ESTIMATED": 0.9,
    "STALE": 0.5,
    "CONFLICT": 0.5,
    "INVALID": 0.0,
}


def quality_coefficient(status: str | None) -> float:
    """数据质量状态 → 权重衰减系数。"""
    if status is None:
        return 1.0
    key = status.value if hasattr(status, "value") else str(status)
    return _QUALITY_COEFF.get(key.upper(), 0.8)


def _utcnow() -> datetime:
    """当前 UTC 时间（带时区）。"""
    return datetime.now(timezone.utc)


def _to_utc(dt: datetime | None) -> datetime:
    """naive datetime 视为 UTC 并附加时区。"""
    if dt is None:
        return _utcnow()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _dec(value: float | None, places: int = 4) -> Decimal | None:
    """float → Decimal（None/NaN/Inf 安全）。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return Decimal(str(round(f, places)))


def _evidence_to_dict(ev: "EngineEvidence") -> dict[str, Any]:
    """EngineEvidence → 可 JSON 序列化字典。"""
    d: dict[str, Any] = {
        "factor": ev.factor,
        "value": ev.value,
        "interpretation": ev.interpretation,
        "weight": ev.weight,
        "supports": ev.supports,
        "confidence": ev.confidence,
        "data_source": ev.data_source,
    }
    if ev.historical_percentile is not None:
        d["historical_percentile"] = ev.historical_percentile
    if ev.indicator_code:
        d["indicator_code"] = ev.indicator_code
    if ev.contribution is not None:
        d["contribution"] = ev.contribution
    if ev.quality_status:
        d["quality_status"] = ev.quality_status
    return d


# --------------------------------------------------------------------------- #
# 证据与输出数据结构
# --------------------------------------------------------------------------- #

@dataclass
class EngineEvidence:
    """引擎证据（单条因子结论）。

    Attributes:
        factor: 因子名称（如 "MVRV 历史分位"）
        value: 当前值（原始数值或描述性字符串）
        interpretation: 普通用户能看懂的解释
        weight: 该因子在融合中的权重（0-1）
        supports: 方向 "bullish" / "bearish" / "neutral"
        confidence: 该证据自身的置信度 0-1
        data_source: 数据来源标识
        historical_percentile: 历史分位（0-100），可选
        indicator_code: 关联指标代码（如 "onchain.mvrv"），可选
        contribution: 对最终评分的贡献值，可选
        quality_status: 数据质量状态，可选
    """

    factor: str
    value: Any
    interpretation: str
    weight: float = 0.0
    supports: str = "neutral"
    confidence: float = 0.5
    data_source: str = ""
    historical_percentile: float | None = None
    indicator_code: str | None = None
    contribution: float | None = None
    quality_status: str | None = None


@dataclass
class EngineOutput:
    """引擎统一输出（可解释性契约，架构文档 §0.2）。

    Attributes:
        state: 当前状态（枚举值字符串）
        confidence: 置信度 0-1
        supporting_evidence: 支持当前状态的证据列表
        opposing_evidence: 反对当前状态的证据列表
        score: 归一化评分（引擎定义域，如 -1~1 或 0~100）
        historical_similar: 历史相似情况列表
        calculated_at: 计算时间
        observation_time: 数据观测时间
        data_quality: 数据质量状态
        explanation: 普通用户解释（一句话中文）
        dimension_scores: 各维度评分字典
        data_gaps: 缺失数据维度声明
        model_version: 模型版本标识
        metadata: 扩展元数据
    """

    state: str
    confidence: float = 0.5
    supporting_evidence: list[EngineEvidence] = field(default_factory=list)
    opposing_evidence: list[EngineEvidence] = field(default_factory=list)
    score: float = 0.0
    historical_similar: list[dict[str, Any]] | None = None
    calculated_at: datetime = field(default_factory=_utcnow)
    observation_time: datetime = field(default_factory=_utcnow)
    data_quality: str = "VERIFIED"
    explanation: str = ""
    dimension_scores: dict[str, float] = field(default_factory=dict)
    data_gaps: list[str] = field(default_factory=list)
    model_version: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def evidence_dicts(self) -> tuple[list[dict], list[dict]]:
        """返回 (supporting, opposing) 的字典列表（用于 JSONB 持久化）。"""
        return (
            [_evidence_to_dict(e) for e in self.supporting_evidence],
            [_evidence_to_dict(e) for e in self.opposing_evidence],
        )

    @property
    def data_coverage(self) -> float:
        """数据覆盖率 = 有效证据权重和 / 全部证据权重和。"""
        all_ev = self.supporting_evidence + self.opposing_evidence
        if not all_ev:
            return 0.0
        total_w = sum(e.weight for e in all_ev)
        active_w = sum(e.weight for e in all_ev if e.confidence > 0)
        return safe_div(active_w, total_w, 0.0)


# --------------------------------------------------------------------------- #
# EngineBase 抽象基类
# --------------------------------------------------------------------------- #

class EngineBase(ABC):
    """业务引擎抽象基类。

    子类需实现：
    - :attr:`name` — 引擎唯一标识
    - :attr:`_orm_model` — 对应 ORM 模型类
    - :meth:`calculate` — 实时/指定时刻计算
    - :meth:`calculate_historical` — 历史区间批量计算

    基类提供：
    - :meth:`save_result` — 持久化到数据库（UPSERT by observation_time）
    - :meth:`get_latest` — 查询最新结果
    - :meth:`get_at_date` — 查询指定日期结果
    - :meth:`_build_output` — 统一输出构建（证据分离 + 置信度计算）
    """

    #: 引擎名称（子类覆写）
    @property
    @abstractmethod
    def name(self) -> str:
        """引擎唯一标识名（如 'cycle' / 'valuation'）。"""
        ...

    #: 对应 ORM 模型类（子类覆写）
    @property
    def _orm_model(self) -> type:
        """返回该引擎对应的 ORM 模型类。"""
        raise NotImplementedError

    #: 观测时间字段名（子类可覆写）
    _time_column: str = "observation_time"

    # ---- 抽象计算接口 ----

    @abstractmethod
    async def calculate(self, as_of: datetime | None = None) -> EngineOutput:
        """计算当前（或指定时刻）状态。

        Args:
            as_of: 观测时刻；None 表示实时计算。

        Returns:
            EngineOutput 统一输出。
        """
        ...

    @abstractmethod
    async def calculate_historical(
        self, start: datetime, end: datetime
    ) -> list[EngineOutput]:
        """计算历史状态序列。

        Args:
            start: 起始时间
            end: 结束时间

        Returns:
            按时间升序的 EngineOutput 列表。
        """
        ...

    # ---- 持久化 ----

    async def save_result(self, output: EngineOutput) -> bool:
        """保存引擎输出到数据库（UPSERT by observation_time）。

        Returns:
            是否成功保存。
        """
        model = self._orm_model
        obs_time = _to_utc(output.observation_time)
        try:
            row_data = self._build_row(output, obs_time)
            async with get_db_session_ctx() as session:
                # 查找已有记录
                stmt = select(model).where(
                    getattr(model, self._time_column) == obs_time
                ).limit(1)
                existing = (await session.execute(stmt)).scalar_one_or_none()
                if existing is not None:
                    for k, v in row_data.items():
                        if k != "id" and hasattr(existing, k):
                            setattr(existing, k, v)
                else:
                    session.add(model(**row_data))
            logger.debug(
                f"[{self.name}] 保存结果: state={output.state} "
                f"confidence={output.confidence:.2f} time={obs_time.isoformat()}"
            )
            return True
        except Exception as exc:
            logger.error(f"[{self.name}] 保存结果失败: {exc!r}")
            return False

    def _build_row(self, output: EngineOutput, obs_time: datetime) -> dict[str, Any]:
        """将 EngineOutput 转为 ORM 行字典（子类可覆写以适配特殊字段）。"""
        supporting, opposing = output.evidence_dicts()
        return {
            self._time_column: obs_time,
            "confidence": _dec(output.confidence),
            "description": output.explanation or None,
            "quality_status": output.data_quality,
        }

    # ---- 查询 ----

    async def get_latest(self) -> EngineOutput | None:
        """获取最新结果（从数据库）。"""
        model = self._orm_model
        try:
            async with get_db_session_ctx() as session:
                stmt = (
                    select(model)
                    .order_by(getattr(model, self._time_column).desc())
                    .limit(1)
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is None:
                    return None
                return self._row_to_output(row)
        except Exception as exc:
            logger.warning(f"[{self.name}] 查询最新结果失败: {exc!r}")
            return None

    async def get_at_date(self, date: datetime) -> EngineOutput | None:
        """获取指定日期（或之前最近）的结果。"""
        model = self._orm_model
        target = _to_utc(date)
        try:
            async with get_db_session_ctx() as session:
                stmt = (
                    select(model)
                    .where(getattr(model, self._time_column) <= target)
                    .order_by(getattr(model, self._time_column).desc())
                    .limit(1)
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is None:
                    return None
                return self._row_to_output(row)
        except Exception as exc:
            logger.warning(f"[{self.name}] 查询 {date} 结果失败: {exc!r}")
            return None

    def _row_to_output(self, row: Any) -> EngineOutput:
        """ORM 行 → EngineOutput（子类可覆写以还原完整证据链）。"""
        return EngineOutput(
            state=getattr(row, "state", getattr(row, "phase", "UNKNOWN")),
            confidence=float(row.confidence) if row.confidence else 0.0,
            observation_time=getattr(row, self._time_column, _utcnow()),
            data_quality=getattr(row, "quality_status", "VERIFIED"),
            explanation=getattr(row, "description", "") or "",
        )

    # ---- 输出构建辅助 ----

    def _build_output(
        self,
        *,
        state: str,
        score: float,
        evidence: list[EngineEvidence],
        explanation: str = "",
        observation_time: datetime | None = None,
        data_quality: str = "VERIFIED",
        dimension_scores: dict[str, float] | None = None,
        historical_similar: list[dict[str, Any]] | None = None,
        data_gaps: list[str] | None = None,
        model_version: str = "",
        metadata: dict[str, Any] | None = None,
        base_confidence: float = 0.8,
    ) -> EngineOutput:
        """统一输出构建器。

        自动完成：
        1. 证据分离（supporting vs opposing，基于 supports 字段与 state 方向）
        2. 置信度计算（因子一致性 × 数据覆盖率 × 质量系数 × base_confidence）
        3. 填充 calculated_at / observation_time
        """
        obs = _to_utc(observation_time)

        # 证据分离：与最终状态方向一致的为 supporting，否则 opposing
        supporting: list[EngineEvidence] = []
        opposing: list[EngineEvidence] = []
        state_dir = self._state_direction(state)
        for ev in evidence:
            ev_dir = ev.supports.lower()
            if ev_dir == state_dir or ev_dir == "neutral":
                supporting.append(ev)
            else:
                opposing.append(ev)

        # 置信度 = base × 一致性 × 覆盖率 × 质量
        consistency = self._factor_consistency(evidence, state_dir)
        coverage = self._data_coverage(evidence)
        avg_quality = self._avg_quality(evidence)
        confidence = clamp(
            base_confidence * consistency * coverage * avg_quality, 0.0, 1.0
        )

        return EngineOutput(
            state=state,
            confidence=round(confidence, 4),
            supporting_evidence=supporting,
            opposing_evidence=opposing,
            score=round(score, 4),
            historical_similar=historical_similar,
            calculated_at=_utcnow(),
            observation_time=obs,
            data_quality=data_quality,
            explanation=explanation,
            dimension_scores=dimension_scores or {},
            data_gaps=data_gaps or [],
            model_version=model_version or f"{self.name}-v1.0.0",
            metadata=metadata or {},
        )

    # ---- 内部辅助 ----

    @staticmethod
    def _state_direction(state: str) -> str:
        """推断状态的方向倾向（子类可覆写）。"""
        s = state.upper()
        bullish_kw = (
            "UPTREND", "ACCELERATION", "RECOVERY", "BULL", "OVERVALUED",
            "EXTREME_OVERVALUED", "INFLOW", "ACCUMULATION", "GREED",
            "EXPANSION", "EUPHORIA", "OPTIMISM", "SUPPORTIVE", "UP",
        )
        bearish_kw = (
            "BEAR", "DECLINE", "DEEP_BEAR", "UNDERVALUED", "DEEP_UNDERVALUED",
            "OUTFLOW", "DISTRIBUTION", "FEAR", "CONTRACTION", "CAPITULATION",
            "HEADWIND", "CRISIS", "DOWN", "TOP_RISK",
        )
        for kw in bullish_kw:
            if kw in s:
                return "bullish"
        for kw in bearish_kw:
            if kw in s:
                return "bearish"
        return "neutral"

    @staticmethod
    def _factor_consistency(evidence: list[EngineEvidence], state_dir: str) -> float:
        """因子一致性：同向因子权重占比（0.3~1.0）。"""
        if not evidence:
            return 0.3
        total_w = sum(e.weight for e in evidence) or 1.0
        aligned_w = sum(
            e.weight for e in evidence
            if e.supports.lower() == state_dir or e.supports.lower() == "neutral"
        )
        ratio = aligned_w / total_w
        return 0.3 + 0.7 * ratio  # 映射到 [0.3, 1.0]

    @staticmethod
    def _data_coverage(evidence: list[EngineEvidence]) -> float:
        """数据覆盖率：有效证据（confidence>0）权重占比。"""
        if not evidence:
            return 0.0
        total_w = sum(e.weight for e in evidence) or 1.0
        active_w = sum(e.weight for e in evidence if e.confidence > 0)
        return max(0.1, active_w / total_w)

    @staticmethod
    def _avg_quality(evidence: list[EngineEvidence]) -> float:
        """平均数据质量系数。"""
        if not evidence:
            return 0.5
        coeffs = [quality_coefficient(e.quality_status) for e in evidence]
        return sum(coeffs) / len(coeffs)

    @staticmethod
    def _split_evidence_by_direction(
        evidence: list[EngineEvidence], positive_dir: str = "bullish"
    ) -> tuple[list[EngineEvidence], list[EngineEvidence]]:
        """按方向分离证据（通用辅助）。"""
        supporting = [e for e in evidence if e.supports.lower() == positive_dir]
        opposing = [e for e in evidence if e.supports.lower() != positive_dir]
        return supporting, opposing
