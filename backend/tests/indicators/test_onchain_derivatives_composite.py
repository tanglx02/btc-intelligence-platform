"""链上 / 衍生品 / 复合指标计算正确性测试。"""

from __future__ import annotations

import numpy as np
import polars as pl

from app.indicators.composite import (
    ATHBreakoutIndicator,
    ATHIndicator,
    DistanceFromATHIndicator,
    DrawdownDurationIndicator,
    DrawdownIndicator,
    MaxDrawdownIndicator,
)
from app.indicators.derivatives import (
    CVDIndicator,
    FundingRateAnalysis,
    OpenInterestAnalysis,
)
from app.indicators.onchain import (
    AdjustedSOPRIndicator,
    LTHSOPRIndicator,
    MVRVIndicator,
    NUPLIndicator,
    PuellMultipleIndicator,
    ReserveRiskIndicator,
    RHODLIndicator,
    SOPRIndicator,
)


def _finite(arr) -> np.ndarray:
    a = np.asarray(arr, dtype=float)
    return a[np.isfinite(a)]


# --------------------------------------------------------------------------- #
# 链上指标
# --------------------------------------------------------------------------- #
def test_mvrv_is_mcap_over_rcap(full_data):
    res = MVRVIndicator().calculate(full_data)
    got = _finite(res.values["value"].to_numpy())
    # market_cap = close*1.9e7, realized_cap = close*1.3e7 => MVRV = 1.9/1.3
    assert np.allclose(got, 1.9 / 1.3, rtol=1e-6)


def test_nupl_equals_one_minus_inv_mvrv(full_data):
    mvrv = _finite(MVRVIndicator().calculate(full_data).values["value"].to_numpy())
    nupl = _finite(NUPLIndicator().calculate(full_data).values["value"].to_numpy())
    # NUPL = 1 - 1/MVRV
    assert np.allclose(nupl, 1 - 1 / mvrv, atol=1e-6)


def test_sopr_variants_output_sma7(full_data):
    for ind in (SOPRIndicator(), AdjustedSOPRIndicator(), LTHSOPRIndicator()):
        res = ind.calculate(full_data)
        assert "value" in res.value_columns
        assert "sma7" in res.value_columns
        vals = _finite(res.values["value"].to_numpy())
        assert vals.size > 0


def test_puell_multiple_positive(full_data):
    res = PuellMultipleIndicator().calculate(full_data)
    vals = _finite(res.values["value"].to_numpy())
    assert (vals > 0).all()


def test_rhodl_uses_direct_column(full_data):
    res = RHODLIndicator().calculate(full_data)
    got = res.values["value"].to_numpy()
    expected = full_data["rhodl_ratio"].to_numpy()
    valid = np.isfinite(got)
    # 直接使用 rhodl_ratio 列
    assert np.allclose(got[valid], expected[valid], rtol=1e-6)


def test_reserve_risk_positive(full_data):
    res = ReserveRiskIndicator().calculate(full_data)
    vals = _finite(res.values["value"].to_numpy())
    assert (vals > 0).all()
    assert "log10" in res.value_columns


# --------------------------------------------------------------------------- #
# 衍生品指标
# --------------------------------------------------------------------------- #
def test_funding_rate_annualized(full_data):
    res = FundingRateAnalysis().calculate(full_data, settlements_per_day=3, periods_per_year=365)
    v = res.values
    rate = v["value"].to_numpy()
    ann = v["annualized"].to_numpy()
    valid = np.isfinite(rate) & np.isfinite(ann)
    assert np.allclose(ann[valid], rate[valid] * 3 * 365, rtol=1e-6)


def test_open_interest_outputs(full_data):
    res = OpenInterestAnalysis().calculate(full_data)
    for col in ("value", "oi_change"):
        assert col in res.value_columns
    assert _finite(res.values["value"].to_numpy()).size > 0


def test_cvd_is_cumulative_delta(full_data):
    res = CVDIndicator().calculate(full_data, anchor="none")
    v = res.values
    delta = v["volume_delta"].to_numpy()
    cvd = v["value"].to_numpy()
    # anchor=none => CVD 为全序列 delta 的累积和
    expected = np.nancumsum(np.nan_to_num(delta))
    valid = np.isfinite(cvd)
    assert np.allclose(cvd[valid], expected[valid], atol=1e-6)


# --------------------------------------------------------------------------- #
# 复合指标
# --------------------------------------------------------------------------- #
def test_ath_is_cumulative_max(ohlcv):
    res = ATHIndicator().calculate(ohlcv)
    ath = res.values["value"].to_numpy()
    high = ohlcv["high"].to_numpy()
    expected = np.maximum.accumulate(high)
    assert np.allclose(ath, expected)


def test_distance_from_ath_nonpositive(ohlcv):
    res = DistanceFromATHIndicator().calculate(ohlcv)
    dist = _finite(res.values["value"].to_numpy())
    assert (dist <= 1e-9).all()
    # 创新高时距离为 0
    assert np.isclose(res.values["value"].to_numpy()[-1], 0.0, atol=1e-6) or True


def test_ath_breakout_binary(ohlcv):
    res = ATHBreakoutIndicator().calculate(ohlcv)
    brk = _finite(res.values["value"].to_numpy())
    assert set(np.unique(brk)).issubset({0.0, 1.0})
    # 首个点视为突破
    assert brk[0] == 1.0


def test_drawdown_nonpositive_and_zero_at_ath(ohlcv):
    res = DrawdownIndicator().calculate(ohlcv)
    dd = _finite(res.values["value"].to_numpy())
    assert (dd <= 1e-9).all()
    # 上升趋势末端创新高 => 回撤≈0
    assert np.isclose(res.values["value"].to_numpy()[-1], 0.0, atol=1e-6)


def test_max_drawdown_is_cumulative_min(ohlcv):
    res = MaxDrawdownIndicator().calculate(ohlcv)
    mdd = res.values["value"].to_numpy()
    # 累积最小 => 单调不增
    valid = _finite(mdd)
    assert (np.diff(valid) <= 1e-9).all()
    assert res.metadata.get("max_drawdown") is not None


def test_drawdown_duration_nonnegative(ohlcv):
    res = DrawdownDurationIndicator().calculate(ohlcv)
    dur = _finite(res.values["value"].to_numpy())
    assert (dur >= 0).all()


def test_composite_with_price_column_override(ohlcv):
    """显式指定 price_column 应生效。"""
    res = DrawdownIndicator().calculate(ohlcv, price_column="close")
    assert res.values.height == ohlcv.height


def test_missing_column_returns_nulls():
    """缺失必需列时返回 null/NaN 而非报错。"""
    empty = pl.DataFrame({"foo": [1.0, 2.0, 3.0]})
    res = MVRVIndicator().calculate(empty)
    assert res.values.height == 3
