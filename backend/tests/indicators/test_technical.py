"""技术指标计算正确性测试（app.indicators.technical）。"""

from __future__ import annotations

import numpy as np
import polars as pl

from app.indicators.technical import (
    ADXIndicator,
    ATRIndicator,
    BollingerBandsIndicator,
    EMAIndicator,
    HistoricalVolatilityIndicator,
    MACDIndicator,
    PercentileRankIndicator,
    RSIIndicator,
    SMAIndicator,
    VWAPIndicator,
    WMAIndicator,
)


def _finite(arr: np.ndarray) -> np.ndarray:
    """过滤掉 NaN / inf，仅保留有限值（指标在数据不足处以 NaN 填充）。"""
    a = np.asarray(arr, dtype=float)
    return a[np.isfinite(a)]


def _ref_wilder_rsi(close: np.ndarray, period: int) -> np.ndarray:
    """规范 Wilder RSI 参考实现（独立于被测代码）。"""
    n = len(close)
    out = np.full(n, np.nan)
    deltas = np.diff(close)
    gains = np.clip(deltas, 0, None)
    losses = np.clip(-deltas, 0, None)
    if len(gains) < period:
        return out

    def _rsi(g: float, loss: float) -> float:
        if loss == 0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + g / loss)

    avg_g = gains[:period].mean()
    avg_l = losses[:period].mean()
    out[period] = _rsi(avg_g, avg_l)
    for i in range(period + 1, n):
        d = deltas[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
        out[i] = _rsi(avg_g, avg_l)
    return out


def test_sma_matches_numpy(ohlcv):
    close = ohlcv["close"].to_numpy()
    period = 20
    res = SMAIndicator().calculate(ohlcv, period=period)
    got = res.values["value"].to_numpy()
    expected = np.convolve(close, np.ones(period) / period, mode="valid")
    assert np.allclose(got[period - 1:], expected)
    assert np.isnan(got[: period - 1]).all()


def test_ema_converges_to_constant():
    df = pl.DataFrame({"close": np.full(100, 50.0)})
    res = EMAIndicator().calculate(df, period=10)
    assert np.isclose(res.last(), 50.0)


def test_wma_manual_value():
    # 窗口 3，权重 [1,2,3]/6；对 [1,2,3] => (1*1+2*2+3*3)/6 = 14/6
    df = pl.DataFrame({"close": [1.0, 2.0, 3.0, 4.0]})
    res = WMAIndicator().calculate(df, period=3)
    got = res.values["value"].to_numpy()
    assert np.isclose(got[2], 14 / 6)
    # 下一窗口 [2,3,4] => (2*1+3*2+4*3)/6 = 20/6
    assert np.isclose(got[3], 20 / 6)


def test_rsi_bounds(rising_series, falling_series, ohlcv):
    up = RSIIndicator().calculate(rising_series, period=14)
    down = RSIIndicator().calculate(falling_series, period=14)
    # 单调上涨 => RSI≈100；单调下跌 => RSI≈0
    assert up.last() > 99.0
    assert down.last() < 1.0
    # 常规数据 RSI 落在 [0,100]
    mid = _finite(RSIIndicator().calculate(ohlcv, period=14).values["value"].to_numpy())
    assert mid.min() >= 0.0 and mid.max() <= 100.0


def test_rsi_matches_wilder_reference(ohlcv):
    """RSI 应逐点匹配规范的 Wilder 平滑参考实现。"""
    period = 14
    close = ohlcv["close"].to_numpy()
    got = RSIIndicator().calculate(ohlcv, period=period).values["value"].to_numpy()
    ref = _ref_wilder_rsi(close, period)
    valid = np.isfinite(ref) & np.isfinite(got)
    assert valid.sum() > 0
    assert np.allclose(got[valid], ref[valid], atol=1e-6)


def test_macd_histogram_is_diff(ohlcv):
    res = MACDIndicator().calculate(ohlcv)
    v = res.values
    macd = v["macd_line"].to_numpy()
    signal = v["signal_line"].to_numpy()
    hist = v["histogram"].to_numpy()
    valid = ~np.isnan(macd) & ~np.isnan(signal) & ~np.isnan(hist)
    assert np.allclose(hist[valid], (macd - signal)[valid])


def test_macd_manual_first_value():
    # 构造线性序列，验证 macd_line = ema12 - ema26 的一致性
    df = pl.DataFrame({"close": np.linspace(100, 200, 60)})
    res = MACDIndicator().calculate(df, fast=12, slow=26, signal=9)
    close = pl.Series(df["close"].to_numpy().astype(float))
    ema12 = close.ewm_mean(span=12, adjust=False).to_numpy()
    ema26 = close.ewm_mean(span=26, adjust=False).to_numpy()
    expected = ema12 - ema26
    got = res.values["macd_line"].to_numpy()
    valid = ~np.isnan(got)
    assert np.allclose(got[valid], expected[valid], atol=1e-6)


def test_atr_positive_and_wilder(ohlcv):
    res = ATRIndicator().calculate(ohlcv, period=14)
    atr = _finite(res.values["value"].to_numpy())
    assert (atr > 0).all()
    # ATR 不应超过 high-low 的最大可能范围的合理上界
    hl = (ohlcv["high"] - ohlcv["low"]).to_numpy()
    assert atr.max() <= hl.max() * 1.5


def test_true_range_formula():
    from app.indicators.technical.atr import true_range

    high = pl.Series([10.0, 12.0, 11.0])
    low = pl.Series([8.0, 9.0, 9.5])
    close = pl.Series([9.0, 11.0, 10.0])
    tr = true_range(high, low, close).to_numpy()
    # 首行退化 H-L=2
    assert np.isclose(tr[0], 2.0)
    # 第 2 行: max(12-9, |12-9|, |9-9|)=3
    assert np.isclose(tr[1], 3.0)


def test_adx_output_columns_and_range(ohlcv):
    res = ADXIndicator().calculate(ohlcv, period=14)
    assert set(res.value_columns) >= {"adx", "plus_di", "minus_di"}
    adx = _finite(res.values["adx"].to_numpy())
    assert (adx >= 0).all() and (adx <= 100).all()


def test_bollinger_middle_is_sma(ohlcv):
    period, k = 20, 2.0
    res = BollingerBandsIndicator().calculate(ohlcv, period=period, std_dev=k)
    v = res.values
    close = ohlcv["close"].to_numpy()
    sma = np.convolve(close, np.ones(period) / period, mode="valid")
    mid = v["middle"].to_numpy()[period - 1:]
    assert np.allclose(mid, sma)
    # upper >= middle >= lower
    up = v["upper"].to_numpy()
    lo = v["lower"].to_numpy()
    valid = ~np.isnan(up)
    assert (up[valid] >= v["middle"].to_numpy()[valid]).all()
    assert (v["middle"].to_numpy()[valid] >= lo[valid]).all()


def test_bollinger_percent_b_and_bandwidth(ohlcv):
    res = BollingerBandsIndicator().calculate(ohlcv, period=20, std_dev=2)
    v = res.values
    up, mid, lo = (v[c].to_numpy() for c in ("upper", "middle", "lower"))
    pb, bw = v["percent_b"].to_numpy(), v["bandwidth"].to_numpy()
    close = ohlcv["close"].to_numpy()
    valid = ~np.isnan(up) & (up != lo)
    assert np.allclose(pb[valid], ((close - lo) / (up - lo))[valid], atol=1e-6)
    assert np.allclose(bw[valid], ((up - lo) / mid)[valid], atol=1e-6)


def test_vwap_within_price_range(ohlcv):
    res = VWAPIndicator().calculate(ohlcv, anchor="none")
    vwap = _finite(res.values["value"].to_numpy())
    assert vwap.min() >= ohlcv["low"].min() - 1e-6
    assert vwap.max() <= ohlcv["high"].max() + 1e-6


def test_hv_nonnegative(ohlcv):
    res = HistoricalVolatilityIndicator().calculate(ohlcv, period=30)
    hv = _finite(res.values["value"].to_numpy())
    assert (hv >= 0).all()


def test_percentile_rank_monotonic_is_100(rising_series):
    df = rising_series.rename({"close": "value"})
    res = PercentileRankIndicator().calculate(df, lookback=None)
    vals = _finite(res.values["value"].to_numpy())
    assert np.allclose(vals, 100.0)


def test_insufficient_data_returns_null_not_raise():
    """数据不足时应返回 null 而非抛异常。"""
    tiny = pl.DataFrame({"close": [1.0, 2.0], "high": [1.5, 2.5], "low": [0.5, 1.5]})
    for ind, params in [
        (SMAIndicator(), {"period": 50}),
        (RSIIndicator(), {"period": 14}),
        (BollingerBandsIndicator(), {"period": 20}),
        (ADXIndicator(), {"period": 14}),
    ]:
        res = ind.calculate(tiny, **params)
        assert res.values.height == 2  # 不报错，行数对齐
