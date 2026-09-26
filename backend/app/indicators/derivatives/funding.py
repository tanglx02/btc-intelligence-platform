"""资金费率分析 Funding Rate Analysis。

对应指标字典 §7.1 ``deriv.funding_rate``。

功能
----
- 资金费率历史序列整理与年化：``年化 Funding = Rate × 每日结算次数 × 365``
  （不同结算周期 8h/4h 的费率不可直接相加，年化后才可对比）；
- 滚动均值（观察资金费率的常态水平）；
- 极端值检测：年化 Funding 持续 >30% 视为多头拥挤，< −20% 视为空头拥挤
  （阈值可配置，见指标字典「历史通常含义」）。

数据来源候选列：``funding_rate`` / ``funding``（单期费率，小数形式，如 0.0001 = 0.01%）。
输出列：value（单期费率）、annualized（年化小数）、annualized_percent、rolling_mean、
extreme（1=多头拥挤 / −1=空头拥挤 / 0=中性）。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from ..base import IndicatorBase, IndicatorCategory, IndicatorResult
from ..utils.math_helpers import rolling_mean, to_float_series

__all__ = ["FundingRateAnalysis", "funding_rate_analysis"]

_FUNDING_CANDIDATES = ("funding_rate", "funding", "funding_rate_avg")


class FundingRateAnalysis(IndicatorBase):
    """资金费率历史分析，``deriv.funding_rate``。"""

    @property
    def name(self) -> str:
        return "deriv.funding_rate"

    @property
    def display_name(self) -> str:
        return "资金费率"

    @property
    def category(self) -> str:
        return IndicatorCategory.DERIVATIVES

    @property
    def required_data(self) -> list[str]:
        return ["funding_rate"]

    @property
    def default_params(self) -> dict[str, Any]:
        return {
            "settlements_per_day": 3,   # 8h 结算 → 每日 3 次
            "periods_per_year": 365,
            "mean_window": 30,
            "long_threshold": 0.30,     # 年化 >30% 多头拥挤
            "short_threshold": -0.20,   # 年化 <−20% 空头拥挤
        }

    @property
    def min_rows(self) -> int:
        return 1

    def validate_input(self, data: pl.DataFrame) -> bool:
        if data is None or data.height == 0:
            return False
        return any(c in data.columns for c in _FUNDING_CANDIDATES)

    def calculate(self, data: pl.DataFrame, **params: Any) -> IndicatorResult:
        """计算资金费率分析。

        Parameters
        ----------
        data:
            含 funding_rate 列与时间列的 DataFrame。
        **params:
            见 :attr:`default_params`。
        """
        p = self._resolve_params(params)
        spd = int(p["settlements_per_day"])
        ppy = int(p["periods_per_year"])
        mean_window = int(p["mean_window"])
        long_th = float(p["long_threshold"])
        short_th = float(p["short_threshold"])

        time_series = self._extract_time(data)
        n = data.height
        if not self.validate_input(data):
            frame = self._empty_frame(n)
            return self._build_result(time_series, frame, params=p)

        _, rate_raw = self._first_column(data, list(_FUNDING_CANDIDATES))
        rate = to_float_series(rate_raw, "funding_rate")
        rate_np = rate.to_numpy().astype("float64")

        annualized = rate_np * spd * ppy
        annualized_pct = annualized * 100.0
        rmean = rolling_mean(rate, mean_window, min_periods=1)

        extreme = np.where(
            annualized > long_th, 1.0, np.where(annualized < short_th, -1.0, 0.0)
        )
        extreme = np.where(np.isfinite(annualized), extreme, np.nan)

        frame = pl.DataFrame(
            {
                "value": rate.alias("value"),
                "annualized": pl.Series("annualized", annualized, dtype=pl.Float64),
                "annualized_percent": pl.Series(
                    "annualized_percent", annualized_pct, dtype=pl.Float64
                ),
                "rolling_mean": rmean.alias("rolling_mean"),
                "extreme": pl.Series("extreme", extreme, dtype=pl.Float64),
            }
        )
        return self._build_result(time_series, frame, params=p)

    @staticmethod
    def _empty_frame(n: int) -> pl.DataFrame:
        cols = ["value", "annualized", "annualized_percent", "rolling_mean", "extreme"]
        return pl.DataFrame({c: pl.Series([None] * n, dtype=pl.Float64) for c in cols})


def funding_rate_analysis(data: pl.DataFrame, **params: Any) -> pl.DataFrame:
    """便捷函数：返回资金费率分析多列 DataFrame。"""
    return FundingRateAnalysis().calculate(data, **params).values
