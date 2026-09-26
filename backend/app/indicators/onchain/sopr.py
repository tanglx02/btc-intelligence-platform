"""花费产出利润率 SOPR 家族（SOPR / aSOPR / LTH-SOPR）。

对应指标字典 §6.3 / §6.4 / §6.5：
``onchain.sopr`` / ``onchain.sopr.adjusted`` / ``onchain.sopr.lth``。

公式
----
    SOPR = Σ(花费 UTXO 产出价值) / Σ(花费 UTXO 创造价值) = 已实现价格 / 创建价格
    aSOPR   = 同 SOPR，但剔除寿命 < 1 小时的 UTXO（过滤链上噪音）
    LTH-SOPR = 仅统计币龄 > 155 天的花费 UTXO 的 SOPR

三者围绕 1 波动：>1 表示当日卖出者平均获利，<1 表示割肉。SOPR 单日噪音大，
引擎消费其 7 日移动平均，故本实现额外输出 ``sma7`` 列。

数据优先直接采集 Provider 公布的 ``sopr`` / ``asopr`` / ``lth_sopr`` 列；
缺失时可由 ``realized_value`` / ``created_value``（花费输出口径）推算。
"""

from __future__ import annotations

from typing import Any

import polars as pl

from ..base import IndicatorBase, IndicatorCategory, IndicatorResult
from ..utils.math_helpers import rolling_mean, safe_divide, to_float_series

__all__ = [
    "SOPRBase",
    "SOPRIndicator",
    "AdjustedSOPRIndicator",
    "LTHSOPRIndicator",
    "sopr",
    "asopr",
    "lth_sopr",
]


class SOPRBase(IndicatorBase):
    """SOPR 家族公共基类。"""

    #: 直接列候选名（子类覆写）
    source_candidates: tuple[str, ...] = ("sopr",)
    #: 推算用分子/分母候选名（花费输出价值 / 创造价值）
    numerator_candidates: tuple[str, ...] = ("realized_value", "spent_output_value")
    denominator_candidates: tuple[str, ...] = ("created_value", "spent_output_created_value")

    @property
    def category(self) -> str:
        return IndicatorCategory.ONCHAIN

    @property
    def default_params(self) -> dict[str, Any]:
        return {"smooth_period": 7}

    @property
    def min_rows(self) -> int:
        return 1

    def validate_input(self, data: pl.DataFrame) -> bool:
        if data is None or data.height == 0:
            return False
        if any(c in data.columns for c in self.source_candidates):
            return True
        return any(c in data.columns for c in self.numerator_candidates) and any(
            c in data.columns for c in self.denominator_candidates
        )

    def _resolve_sopr(self, data: pl.DataFrame) -> pl.Series:
        """解析 SOPR 序列：优先直接列，其次由 分子/分母 推算。"""
        for cand in self.source_candidates:
            if cand in data.columns:
                return to_float_series(data[cand], "value")
        num_name, _ = self._first_column(data, list(self.numerator_candidates))
        den_name, _ = self._first_column(data, list(self.denominator_candidates))
        if num_name and den_name:
            num = data[num_name].cast(pl.Float64, strict=False)
            den = data[den_name].cast(pl.Float64, strict=False)
            return safe_divide(num, den, default=float("nan"))
        return pl.Series("value", [None] * data.height, dtype=pl.Float64)

    def calculate(self, data: pl.DataFrame, **params: Any) -> IndicatorResult:
        """计算 SOPR 及其移动平均。

        Parameters
        ----------
        data:
            含 SOPR 直接列（或可推算的分子/分母列）与时间列的 DataFrame。
        **params:
            ``smooth_period``：移动平均窗口，默认 7 天。
        """
        p = self._resolve_params(params)
        smooth_period = int(p.get("smooth_period", 7) or 7)

        time_series = self._extract_time(data)
        n = data.height
        if not self.validate_input(data):
            frame = pl.DataFrame(
                {
                    "value": pl.Series([None] * n, dtype=pl.Float64),
                    "sma7": pl.Series([None] * n, dtype=pl.Float64),
                }
            )
            return self._build_result(time_series, frame, params={"smooth_period": smooth_period})

        sopr_series = self._resolve_sopr(data).alias("value")
        smoothed = rolling_mean(sopr_series, smooth_period, min_periods=1).alias("sma7")
        frame = pl.DataFrame({"value": sopr_series, "sma7": smoothed})
        return self._build_result(
            time_series, frame, params={"smooth_period": smooth_period}
        )


class SOPRIndicator(SOPRBase):
    """花费产出利润率，``onchain.sopr``。"""

    source_candidates = ("sopr",)

    @property
    def name(self) -> str:
        return "onchain.sopr"

    @property
    def display_name(self) -> str:
        return "花费产出利润率"

    @property
    def required_data(self) -> list[str]:
        return ["sopr"]


class AdjustedSOPRIndicator(SOPRBase):
    """调整花费产出利润率（剔除 <1h UTXO），``onchain.sopr.adjusted``。"""

    source_candidates = ("asopr", "adjusted_sopr", "sopr_adjusted", "sopr")

    @property
    def name(self) -> str:
        return "onchain.sopr.adjusted"

    @property
    def display_name(self) -> str:
        return "调整花费产出利润率"

    @property
    def required_data(self) -> list[str]:
        return ["asopr"]


class LTHSOPRIndicator(SOPRBase):
    """长期持有者花费产出利润率（币龄 > 155 天），``onchain.sopr.lth``。"""

    source_candidates = ("lth_sopr", "sopr_lth", "lthsopr")

    @property
    def name(self) -> str:
        return "onchain.sopr.lth"

    @property
    def display_name(self) -> str:
        return "长期持有者花费产出利润率"

    @property
    def required_data(self) -> list[str]:
        return ["lth_sopr"]


# --------------------------------------------------------------------------- #
# 便捷函数
# --------------------------------------------------------------------------- #
def sopr(data: pl.DataFrame, smooth_period: int = 7) -> pl.Series:
    """便捷函数：返回 SOPR 序列。"""
    return SOPRIndicator().calculate(data, smooth_period=smooth_period).values["value"]


def asopr(data: pl.DataFrame, smooth_period: int = 7) -> pl.Series:
    """便捷函数：返回 aSOPR 序列。"""
    return AdjustedSOPRIndicator().calculate(data, smooth_period=smooth_period).values["value"]


def lth_sopr(data: pl.DataFrame, smooth_period: int = 7) -> pl.Series:
    """便捷函数：返回 LTH-SOPR 序列。"""
    return LTHSOPRIndicator().calculate(data, smooth_period=smooth_period).values["value"]
