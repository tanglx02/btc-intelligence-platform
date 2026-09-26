"""历史百分位排名 Percentile Rank。

对应指标字典 §5.10 ``tech.percentile_rank``（通用派生工具，全部引擎的主要输入形式）。

公式
----
    PctRank(x_t, N) = count(x_i < x_t, i ∈ [t−N, t)) / N × 100%

支持两种模式：

- **滚动窗口**（``lookback = N``）：当前值在最近 N 期历史（不含自身）中的百分位；
- **全历史扩展**（``lookback = None``）：从序列起点累积计算百分位（FULL_HISTORY）。

输出范围 0–100。可作用于任意数值列（默认自动探测 ``value`` 或 ``close``）。
"""

from __future__ import annotations

from typing import Any

import polars as pl

from ..base import IndicatorBase, IndicatorCategory, IndicatorResult
from ..utils.math_helpers import (
    expanding_percentile_rank,
    rolling_percentile_rank,
    to_float_series,
)

__all__ = ["PercentileRankIndicator", "percentile_rank_series"]


class PercentileRankIndicator(IndicatorBase):
    """历史百分位排名，``tech.percentile_rank``。"""

    #: 默认目标列（不存在时回退到 close）
    value_column: str = "value"

    @property
    def name(self) -> str:
        return "tech.percentile_rank"

    @property
    def display_name(self) -> str:
        return "历史百分位排名"

    @property
    def category(self) -> str:
        return IndicatorCategory.TECHNICAL

    @property
    def required_data(self) -> list[str]:
        # 通用工具：value 或 close 任一即可，validate_input 中放宽校验
        return [self.value_column]

    @property
    def default_params(self) -> dict[str, Any]:
        return {"lookback": None, "column": None}

    @property
    def min_rows(self) -> int:
        return 2

    def validate_input(self, data: pl.DataFrame) -> bool:
        """放宽校验：只要存在 ``value`` 或 ``close`` 任一列即视为有效。"""
        if data is None or data.height == 0:
            return False
        return self.value_column in data.columns or "close" in data.columns

    def _resolve_column(self, data: pl.DataFrame, column: str | None) -> str | None:
        if column and column in data.columns:
            return column
        if self.value_column in data.columns:
            return self.value_column
        if "close" in data.columns:
            return "close"
        return None

    def calculate(self, data: pl.DataFrame, **params: Any) -> IndicatorResult:
        """计算百分位排名。

        Parameters
        ----------
        data:
            含目标数值列（``value`` / ``close`` 或显式 ``column``）与时间列的 DataFrame。
        **params:
            ``lookback``：滚动窗口 N；``None`` 或 <=0 表示全历史扩展百分位；
            ``column``：显式指定目标列名。
        """
        p = self._resolve_params(params)
        lookback = p.get("lookback")
        column = p.get("column")

        time_series = self._extract_time(data)
        n = data.height
        target_col = self._resolve_column(data, column)

        if target_col is None or n == 0:
            frame = pl.DataFrame({"value": pl.Series([None] * n, dtype=pl.Float64)})
            return self._build_result(
                time_series, frame, params={"lookback": lookback, "column": target_col}
            )

        series = to_float_series(data[target_col], target_col)
        if lookback is not None and int(lookback) > 0:
            pct = rolling_percentile_rank(series, int(lookback))
            mode = "rolling_window"
        else:
            pct = expanding_percentile_rank(series)
            mode = "full_history"

        frame = pl.DataFrame({"value": pct.alias("value")})
        return self._build_result(
            time_series,
            frame,
            params={"lookback": lookback, "column": target_col},
            extra_metadata={"percentile_method": mode, "target_column": target_col},
        )


def percentile_rank_series(
    data: pl.DataFrame | pl.Series, lookback: int | None = None, column: str = "close"
) -> pl.Series:
    """便捷函数：返回百分位排名序列（0–100）。"""
    if isinstance(data, pl.DataFrame):
        df = data
    else:
        df = pl.DataFrame({column: data})
    return PercentileRankIndicator().calculate(df, lookback=lookback, column=column).values["value"]
