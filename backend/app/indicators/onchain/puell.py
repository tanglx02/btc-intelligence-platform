"""普尔倍数 Puell Multiple。

对应指标字典 §6.7 ``onchain.puell_multiple``。

公式
----
    Puell = 当日矿工收入(USD) / 365 日移动平均矿工收入(USD)

矿工收入 = 区块奖励 + 手续费（按产出当日价格计 USD）。MA 窗口固定 365 天。
>4 历史上多次对应周期顶部附近；<0.5 多次对应底部区域。

⚠️ 口径陷阱：比特币减半会使矿工收入骤降，Puell 在减半后 1 年内系统性偏低，
解读必须结合减半日历。

数据来源候选列：``miner_revenue_usd`` / ``daily_issuance_usd`` / ``issuance_usd`` /
``miner_revenue`` / ``daily_issuance``。
"""

from __future__ import annotations

from typing import Any

import polars as pl

from ..base import IndicatorBase, IndicatorCategory, IndicatorResult
from ..utils.math_helpers import rolling_mean, safe_divide, to_float_series

__all__ = ["PuellMultipleIndicator", "puell_multiple"]

# 矿工收入 / 日发行量候选列名
_REVENUE_CANDIDATES = (
    "miner_revenue_usd",
    "daily_issuance_usd",
    "issuance_usd",
    "miner_revenue",
    "daily_issuance",
    "issuance",
)


class PuellMultipleIndicator(IndicatorBase):
    """普尔倍数，``onchain.puell_multiple``。"""

    @property
    def name(self) -> str:
        return "onchain.puell_multiple"

    @property
    def display_name(self) -> str:
        return "普尔倍数"

    @property
    def category(self) -> str:
        return IndicatorCategory.ONCHAIN

    @property
    def required_data(self) -> list[str]:
        return ["miner_revenue_usd"]

    @property
    def default_params(self) -> dict[str, Any]:
        return {"ma_period": 365, "min_periods": 365}

    @property
    def min_rows(self) -> int:
        return 1

    def validate_input(self, data: pl.DataFrame) -> bool:
        if data is None or data.height == 0:
            return False
        return any(c in data.columns for c in _REVENUE_CANDIDATES)

    def calculate(self, data: pl.DataFrame, **params: Any) -> IndicatorResult:
        """计算 Puell Multiple。

        Parameters
        ----------
        data:
            含矿工收入列与时间列的 DataFrame。
        **params:
            ``ma_period``：移动平均窗口，默认 365；
            ``min_periods``：产生有效均值所需最小样本数，默认等于 ``ma_period``。
        """
        p = self._resolve_params(params)
        ma_period = int(p["ma_period"])
        min_periods = int(p.get("min_periods", ma_period) or ma_period)

        time_series = self._extract_time(data)
        n = data.height
        if not self.validate_input(data):
            frame = pl.DataFrame({"value": pl.Series([None] * n, dtype=pl.Float64)})
            return self._build_result(
                time_series, frame, params={"ma_period": ma_period, "min_periods": min_periods}
            )

        revenue = to_float_series(self._first_column(data, list(_REVENUE_CANDIDATES))[1], "revenue")
        ma = rolling_mean(revenue, ma_period, min_periods=min_periods)
        value = safe_divide(revenue, ma, default=float("nan")).alias("value")

        frame = pl.DataFrame({"value": value})
        return self._build_result(
            time_series,
            frame,
            params={"ma_period": ma_period, "min_periods": min_periods},
        )


def puell_multiple(data: pl.DataFrame, ma_period: int = 365) -> pl.Series:
    """便捷函数：返回 Puell Multiple 序列。"""
    return PuellMultipleIndicator().calculate(data, ma_period=ma_period).values["value"]
