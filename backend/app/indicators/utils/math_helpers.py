"""数学工具函数。

为指标计算提供高性能、可复用的基础数学原语。所有函数：

- 统一以 ``float64`` 精度计算；
- 数据不足时返回 ``null``（Polars）或 ``NaN``，绝不抛异常；
- 同时支持标量与 :class:`polars.Series` 输入（``safe_divide`` 等）。

设计约定
--------
本模块只在 **Series / 标量** 层面运算。指标类的 ``calculate`` 方法负责从
输入 DataFrame 中抽取列 -> 调用本模块原语 -> 重新组装结果 DataFrame，
从而保持计算逻辑与数据结构解耦、便于独立单元测试。
"""

from __future__ import annotations

import math
from typing import Any, Union

import numpy as np
import polars as pl

__all__ = [
    "to_float_series",
    "wilder_smooth",
    "rolling_mean",
    "rolling_std",
    "log_returns",
    "simple_returns",
    "annualize",
    "percentile_rank",
    "rolling_percentile_rank",
    "expanding_percentile_rank",
    "rolling_correlation",
    "safe_divide",
    "z_score",
    "rolling_z_score",
]

# 支持作为「数值序列」传入的类型别名
Numeric = Union[pl.Series, "np.ndarray[Any, Any]", list[float], float, int, None]

# 每年周期数：加密货币 7×24 交易，日线年化因子统一使用 365
TRADING_DAYS_PER_YEAR = 365


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _is_series(value: Any) -> bool:
    """判断输入是否为 Polars Series。"""
    return isinstance(value, pl.Series)


def _to_numpy(value: Numeric) -> np.ndarray[Any, Any] | float:
    """将输入转换为 float64 numpy 数组；标量原样返回（供 numpy 广播）。"""
    if value is None:
        return np.nan
    if _is_series(value):
        return value.cast(pl.Float64, strict=False).to_numpy().astype("float64")  # type: ignore[union-attr]
    if isinstance(value, np.ndarray):
        return value.astype("float64")
    if isinstance(value, (list, tuple)):
        return np.asarray(value, dtype="float64")
    # 标量
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return np.nan


def to_float_series(values: Numeric, name: str = "value") -> pl.Series:
    """将任意数值序列统一转换为 ``float64`` 的 :class:`polars.Series`。

    Parameters
    ----------
    values:
        Polars Series / numpy 数组 / list / 标量。
    name:
        输出 Series 名称（当输入无名称时使用）。

    Returns
    -------
    polars.Series
        float64 类型序列。标量会被包装为单元素序列。
    """
    if _is_series(values):
        return values.cast(pl.Float64, strict=False).alias(name or values.name)  # type: ignore[union-attr]
    arr = _to_numpy(values)
    if np.isscalar(arr):
        return pl.Series(name, [arr], dtype=pl.Float64)
    return pl.Series(name, arr, dtype=pl.Float64)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 平滑 / 移动统计
# --------------------------------------------------------------------------- #
def wilder_smooth(values: Numeric, period: int) -> pl.Series:
    """Wilder 平滑（Wilder's Smoothing Method）。

    公式（与 RSI / ATR / ADX 一致）::

        seed      = SMA(x[0..period-1])                     # 首值为 N 期简单平均
        smooth_t  = (smooth_{t-1} * (period - 1) + x_t) / period   # t >= period

    等价于 ``alpha = 1 / period`` 的 EMA，但 **以前 N 期简单平均为种子**，
    前 ``period - 1`` 个位置返回 ``null``。实现上借助 Polars ``ewm_mean``
    的 ``ignore_nulls`` 特性完成向量化计算（避免 Python 循环）。

    Parameters
    ----------
    values:
        待平滑序列。
    period:
        平滑周期 N（正整数）。

    Returns
    -------
    polars.Series
        平滑后的 float64 序列，长度与输入一致。
    """
    s = to_float_series(values)
    n = s.len()
    period = int(period)
    if period <= 0:
        raise ValueError("period 必须为正整数")
    if n == 0:
        return pl.Series("value", [], dtype=pl.Float64)
    if n < period:
        # 数据不足：全部返回 null
        return pl.Series(s.name, [None] * n, dtype=pl.Float64)

    seed_idx = period - 1
    seed = s.head(period).mean()
    tail_values = s.slice(period).to_numpy().astype("float64")

    # 前导位置必须使用 **null**（而非 NaN），否则 ewm_mean 的 ignore_nulls
    # 无法跳过它们、会导致 NaN 沿 EMA 递推污染整个序列。
    name = s.name or "value"
    leading = pl.Series(name, [None] * seed_idx, dtype=pl.Float64)
    seed_series = pl.Series(name, [seed], dtype=pl.Float64)
    tail_series = pl.Series(name, tail_values, dtype=pl.Float64)
    seeded = pl.concat([leading, seed_series, tail_series])

    # ignore_nulls=True 使前导 null 被跳过，EMA 从种子位置开始递推 —— 即 Wilder 平滑
    return seeded.ewm_mean(alpha=1.0 / period, adjust=False, ignore_nulls=True)


def rolling_mean(values: Numeric, period: int, min_periods: int | None = None) -> pl.Series:
    """滚动简单移动平均（SMA）。

    Parameters
    ----------
    values:
        输入序列。
    period:
        窗口大小 N。
    min_periods:
        产生有效值所需的最小样本数，默认等于 ``period``（严格 SMA）。

    Returns
    -------
    polars.Series
        前 ``period - 1`` 个位置为 ``null``。
    """
    s = to_float_series(values)
    period = int(period)
    if period <= 0:
        raise ValueError("period 必须为正整数")
    if s.len() == 0:
        return pl.Series("value", [], dtype=pl.Float64)
    mp = period if min_periods is None else int(min_periods)
    return s.rolling_mean(window_size=period, min_samples=mp)


def rolling_std(
    values: Numeric, period: int, ddof: int = 1, min_periods: int | None = None
) -> pl.Series:
    """滚动标准差。

    Parameters
    ----------
    values:
        输入序列。
    period:
        窗口大小 N。
    ddof:
        自由度校正，``1`` 为样本标准差（布林带默认），``0`` 为总体标准差。
    min_periods:
        最小样本数，默认等于 ``period``。

    Returns
    -------
    polars.Series
        滚动窗口标准差序列。
    """
    s = to_float_series(values)
    period = int(period)
    if period <= 0:
        raise ValueError("period 必须为正整数")
    if s.len() == 0:
        return pl.Series("value", [], dtype=pl.Float64)
    mp = period if min_periods is None else int(min_periods)
    return s.rolling_std(window_size=period, ddof=ddof, min_samples=mp)


# --------------------------------------------------------------------------- #
# 收益率 / 波动率
# --------------------------------------------------------------------------- #
def log_returns(prices: Numeric) -> pl.Series:
    """对数收益率。

    公式::

        r_t = ln(P_t / P_{t-1})

    首个位置返回 ``null``；价格 <= 0 的位置返回 ``null``（对数无定义）。

    Parameters
    ----------
    prices:
        价格序列。

    Returns
    -------
    polars.Series
        对数收益率序列，长度与输入一致。
    """
    s = to_float_series(prices)
    n = s.len()
    if n == 0:
        return pl.Series("value", [], dtype=pl.Float64)
    arr = s.to_numpy().astype("float64")
    out = np.empty(n, dtype="float64")
    out[0] = np.nan
    if n > 1:
        prev = arr[:-1]
        cur = arr[1:]
        with np.errstate(divide="ignore", invalid="ignore"):
            valid = (prev > 0) & (cur > 0) & np.isfinite(prev) & np.isfinite(cur)
            ratio = np.where(valid, cur / np.where(prev == 0, np.nan, prev), np.nan)
            out[1:] = np.log(ratio)
    return pl.Series(s.name, out, dtype=pl.Float64)


def simple_returns(prices: Numeric) -> pl.Series:
    """简单（算术）收益率 ``r_t = P_t / P_{t-1} - 1``。

    首个位置返回 ``null``。
    """
    s = to_float_series(prices)
    n = s.len()
    if n == 0:
        return pl.Series("value", [], dtype=pl.Float64)
    arr = s.to_numpy().astype("float64")
    out = np.empty(n, dtype="float64")
    out[0] = np.nan
    if n > 1:
        prev = arr[:-1]
        with np.errstate(divide="ignore", invalid="ignore"):
            out[1:] = np.where(prev != 0, arr[1:] / prev - 1.0, np.nan)
    return pl.Series(s.name, out, dtype=pl.Float64)


def annualize(values: Numeric, periods_per_year: int = TRADING_DAYS_PER_YEAR) -> pl.Series:
    """将「每期标准差 / 波动率」年化。

    公式::

        annualized = value * sqrt(periods_per_year)

    加密货币全年交易，``periods_per_year`` 默认取 365。若传入的是收益率序列，
    应先自行求标准差再调用本函数。

    Parameters
    ----------
    values:
        每期波动率（标准差）序列或标量。
    periods_per_year:
        年化因子周期数（日线 365，小时线 365*24）。

    Returns
    -------
    polars.Series 或 float
        与输入同类型的年化结果。
    """
    factor = math.sqrt(float(periods_per_year))
    if _is_series(values) or isinstance(values, (list, tuple, np.ndarray)):
        return to_float_series(values) * factor
    try:
        return float(values) * factor  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float("nan")


# --------------------------------------------------------------------------- #
# 分位数 / 标准化
# --------------------------------------------------------------------------- #
def percentile_rank(values: Numeric, current: float | None = None) -> float:
    """当前值在给定历史样本中的百分位排名（0~100）。

    公式（严格小于计数法，见指标字典 §5.10）::

        PctRank(x_t, N) = count(x_i < x_t, i in sample) / N * 100%

    Parameters
    ----------
    values:
        历史样本序列。若 ``current`` 为 ``None``，则取序列最后一个元素作为当前值，
        样本为 **除最后一个元素之外** 的全部历史（Point-in-Time，避免自身计入）。
    current:
        显式指定的当前值。提供时 ``values`` 全部作为历史样本。

    Returns
    -------
    float
        百分位（0~100）；样本不足时返回 ``NaN``。
    """
    s = to_float_series(values)
    arr = s.to_numpy().astype("float64")
    if current is None:
        if arr.size < 2:
            return float("nan")
        target = float(arr[-1])
        sample = arr[:-1]
    else:
        target = float(current)
        sample = arr
    sample = sample[np.isfinite(sample)]
    if sample.size == 0 or not math.isfinite(target):
        return float("nan")
    less = int(np.sum(sample < target))
    return less / sample.size * 100.0


def rolling_percentile_rank(values: Numeric, window: int) -> pl.Series:
    """滚动窗口百分位排名。

    对每个位置 ``t``，统计窗口 ``[t-window, t)``（**不含 t 自身**）内小于
    ``x_t`` 的比例。窗口内有效样本不足时返回 ``null``。

    Parameters
    ----------
    values:
        输入序列。
    window:
        回看窗口长度 N。

    Returns
    -------
    polars.Series
        百分位（0~100）序列。
    """
    s = to_float_series(values)
    arr = s.to_numpy().astype("float64")
    n = arr.size
    window = int(window)
    if window <= 0:
        raise ValueError("window 必须为正整数")
    out = np.full(n, np.nan, dtype="float64")
    for i in range(n):
        start = max(0, i - window)
        sample = arr[start:i]
        sample = sample[np.isfinite(sample)]
        target = arr[i]
        if sample.size == 0 or not np.isfinite(target):
            continue
        out[i] = np.sum(sample < target) / sample.size * 100.0
    return pl.Series(s.name, out, dtype=pl.Float64)


def expanding_percentile_rank(values: Numeric, min_samples: int = 2) -> pl.Series:
    """扩展窗口（全历史累积）百分位排名。

    对每个位置 ``t``，使用 ``[0, t)`` 的全部历史（不含 t 自身）计算分位，
    对应指标字典 §4.1 的 ``FULL_HISTORY`` / ``EXPANDING_FROM`` 方法。

    Parameters
    ----------
    values:
        输入序列。
    min_samples:
        产生有效分位所需的最小历史样本数，默认 2。

    Returns
    -------
    polars.Series
        百分位（0~100）序列，历史不足处为 ``null``。
    """
    s = to_float_series(values)
    arr = s.to_numpy().astype("float64")
    n = arr.size
    out = np.full(n, np.nan, dtype="float64")
    # 使用排序列表 + 二分查找可将复杂度降至 O(n log n)
    seen: list[float] = []
    for i in range(n):
        target = arr[i]
        if np.isfinite(target) and len(seen) >= min_samples:
            import bisect

            less = bisect.bisect_left(seen, target)
            out[i] = less / len(seen) * 100.0
        if np.isfinite(target):
            import bisect

            bisect.insort(seen, target)
    return pl.Series(s.name, out, dtype=pl.Float64)


def rolling_correlation(x: Numeric, y: Numeric, window: int) -> pl.Series:
    """滚动皮尔逊相关系数。

    对每个位置 ``t``，计算窗口 ``[t-window+1, t]``（含 t）内 x 与 y 的相关系数。
    窗口内有效样本 < 2 或任一序列方差为 0 时返回 ``null``。

    Parameters
    ----------
    x, y:
        两个等长序列。
    window:
        滚动窗口长度。

    Returns
    -------
    polars.Series
        相关系数（-1~1）序列。
    """
    xs = to_float_series(x)
    ys = to_float_series(y)
    xa = xs.to_numpy().astype("float64")
    ya = ys.to_numpy().astype("float64")
    n = min(xa.size, ya.size)
    window = int(window)
    if window <= 0:
        raise ValueError("window 必须为正整数")
    out = np.full(xa.size, np.nan, dtype="float64")
    for i in range(n):
        start = max(0, i - window + 1)
        xw = xa[start:i + 1]
        yw = ya[start:i + 1]
        mask = np.isfinite(xw) & np.isfinite(yw)
        xw = xw[mask]
        yw = yw[mask]
        if xw.size < 2:
            continue
        xstd = np.std(xw, ddof=1)
        ystd = np.std(yw, ddof=1)
        if xstd == 0 or ystd == 0:
            continue
        corr = np.corrcoef(xw, yw)[0, 1]
        out[i] = float(corr) if np.isfinite(corr) else np.nan
    name = xs.name if xs.name else "corr"
    return pl.Series(name, out, dtype=pl.Float64)


def safe_divide(a: Numeric, b: Numeric, default: float = 0.0) -> Any:
    """安全除法：分母为 0 / NaN / inf 时返回 ``default``，绝不抛异常。

    支持标量与序列混合输入（借助 numpy 广播）。

    Parameters
    ----------
    a:
        分子。
    b:
        分母。
    default:
        非法除法时的替代值，默认 ``0.0``。

    Returns
    -------
    float 或 polars.Series
        若 a、b 均为标量则返回 float，否则返回 Series。
    """
    a_series = _is_series(a)
    b_series = _is_series(b)
    a_is_seq = a_series or isinstance(a, (list, tuple, np.ndarray))
    b_is_seq = b_series or isinstance(b, (list, tuple, np.ndarray))

    if not (a_is_seq or b_is_seq):
        # 纯标量路径
        try:
            av = float(a)  # type: ignore[arg-type]
            bv = float(b)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default
        if bv == 0.0 or not math.isfinite(bv) or not math.isfinite(av):
            return default
        return av / bv

    # 序列路径
    av = _to_numpy(a)
    bv = _to_numpy(b)
    with np.errstate(divide="ignore", invalid="ignore"):
        denom_ok = (bv != 0) & np.isfinite(bv)
        numer_ok = np.isfinite(av) if isinstance(av, np.ndarray) else math.isfinite(float(av))
        raw = np.where(denom_ok & numer_ok, av / np.where(denom_ok, bv, 1.0), default)
    name = a.name if a_series else (b.name if b_series else "value")  # type: ignore[union-attr]
    return pl.Series(name, raw.astype("float64"), dtype=pl.Float64)


def z_score(values: Numeric, mean: float, std: float) -> Any:
    """Z-Score 标准化 ``(x - mean) / std``；``std`` 为 0 时返回 0。"""
    return safe_divide(
        to_float_series(values) - mean if _is_series(values) else values,  # type: ignore[arg-type]
        std,
        default=0.0,
    )


def rolling_z_score(values: Numeric, window: int = 365, ddof: int = 1) -> pl.Series:
    """滚动 Z-Score：``(x_t - rolling_mean) / rolling_std``（见指标字典 §4.2）。

    Parameters
    ----------
    values:
        输入序列。
    window:
        滚动窗口，默认 365 天。
    ddof:
        标准差自由度校正。

    Returns
    -------
    polars.Series
        Z-Score 序列，窗口不足处为 ``null``。
    """
    s = to_float_series(values)
    mean = rolling_mean(s, window)
    std = rolling_std(s, window, ddof=ddof)
    result = safe_divide(s - mean, std, default=float("nan"))
    return result.alias(s.name)
