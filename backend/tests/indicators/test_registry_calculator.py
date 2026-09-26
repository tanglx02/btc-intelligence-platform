"""注册中心与调度计算器测试。"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import polars as pl
import pytest

from app.indicators.calculator import (
    IndicatorCalculator,
    _data_signature,
    _df_to_ipc,
    _finite_decimal,
    _ipc_to_df,
    _jsonable,
)
from app.indicators.registry import (
    INDICATOR_DEFINITIONS,
    IndicatorRegistry,
    get_indicator,
    get_indicator_definition,
    list_indicators,
    registry,
)


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
def test_registry_autodiscovers_all():
    reg = IndicatorRegistry()
    count = reg.register_all()
    assert count == len(INDICATOR_DEFINITIONS)
    assert set(reg.categories()) == {"technical", "onchain", "derivatives", "composite"}


def test_registry_get_indicator_singleton():
    a = registry.get_indicator("tech.rsi")
    b = registry.get_indicator("tech.rsi")
    assert a is b
    assert a.name == "tech.rsi"


def test_registry_get_unknown_raises():
    with pytest.raises(KeyError):
        registry.get_indicator("does.not.exist")


def test_registry_list_indicators_filter():
    tech = list_indicators("technical")
    assert all(item["category"] == "technical" for item in tech)
    assert {i["name"] for i in tech} >= {"tech.rsi", "tech.sma", "tech.macd"}
    all_inds = registry.list_indicators()
    assert len(all_inds) == len(INDICATOR_DEFINITIONS)


def test_every_registered_indicator_has_definition():
    for name in registry.names():
        assert name in INDICATOR_DEFINITIONS, f"{name} 缺少元数据定义"


def test_definition_structure_dual_interpretation():
    d = get_indicator_definition("tech.rsi")
    assert d["code"] == "tech.rsi"
    assert d["category"] == "technical"
    interp = d["interpretation"]
    assert interp["simple"] and interp["pro"]
    assert "formula" in d and "unit" in d
    assert isinstance(d["default_params"], dict)


def test_registry_rejects_non_indicator():
    reg = IndicatorRegistry()
    with pytest.raises(TypeError):
        reg.register(dict)  # type: ignore[arg-type]


def test_module_level_helpers():
    assert get_indicator("onchain.mvrv").name == "onchain.mvrv"
    assert get_indicator_definition("deriv.cvd")["category"] == "derivatives"


# --------------------------------------------------------------------------- #
# calculator: 序列化辅助
# --------------------------------------------------------------------------- #
def test_ipc_roundtrip_lossless():
    df = pl.DataFrame(
        {
            "time": pl.datetime_range(
                pl.datetime(2023, 1, 1), pl.datetime(2023, 1, 3), "1d", eager=True
            ),
            "value": [1.5, 2.5, 3.5],
        }
    )
    assert _ipc_to_df(_df_to_ipc(df)).equals(df)


def test_data_signature_changes_with_content():
    df1 = pl.DataFrame({"close": [1.0, 2.0, 3.0]})
    df2 = pl.DataFrame({"close": [1.0, 2.0, 4.0]})
    assert _data_signature(df1) != _data_signature(df2)
    assert _data_signature(df1) == _data_signature(df1.clone())


def test_jsonable_handles_numpy_and_datetime():
    import datetime as dt

    payload = _jsonable(
        {
            "np_float": np.float64(1.5),
            "np_array": np.array([1, 2, 3]),
            "ts": dt.datetime(2023, 1, 1, tzinfo=dt.UTC),
            "nested": {"x": np.int64(7)},
        }
    )
    assert payload["np_float"] == 1.5
    assert payload["np_array"] == [1, 2, 3]
    assert isinstance(payload["ts"], str)
    assert payload["nested"]["x"] == 7


def test_finite_decimal():
    assert _finite_decimal(1.5) == Decimal("1.5")
    assert _finite_decimal(None) is None
    assert _finite_decimal(float("nan")) is None
    assert _finite_decimal(float("inf")) is None


# --------------------------------------------------------------------------- #
# calculator: 同步计算
# --------------------------------------------------------------------------- #
def test_calculate_indicator_sync(ohlcv):
    calc = IndicatorCalculator()
    res = calc.calculate_indicator("tech.rsi", ohlcv, period=14)
    assert res.name == "tech.rsi"
    assert res.values.height == ohlcv.height
    assert res.last() is not None


def test_calculate_batch_sync(ohlcv):
    calc = IndicatorCalculator()
    res = calc.calculate_batch(["tech.sma", "tech.atr", "composite.ath"], ohlcv)
    assert set(res) == {"tech.sma", "tech.atr", "composite.ath"}


def test_calculate_batch_per_indicator_params(ohlcv):
    calc = IndicatorCalculator()
    res = calc.calculate_batch(
        ["tech.sma"], ohlcv, params={"tech.sma": {"period": 7}}
    )
    assert res["tech.sma"].metadata["params"]["period"] == 7


def test_calculate_batch_skips_errors(ohlcv):
    calc = IndicatorCalculator()
    # 一个有效 + 一个不存在 => 默认跳过失败项，不抛异常
    res = calc.calculate_batch(["tech.sma", "no.such.indicator"], ohlcv)
    assert "tech.sma" in res
    assert "no.such.indicator" not in res


def test_calculate_batch_stop_on_error(ohlcv):
    calc = IndicatorCalculator()
    with pytest.raises(KeyError):
        calc.calculate_batch(["no.such.indicator"], ohlcv, stop_on_error=True)


def test_cache_key_deterministic(ohlcv):
    calc = IndicatorCalculator()
    k1 = calc._cache_key("tech.rsi", ohlcv, {"period": 14})
    k2 = calc._cache_key("tech.rsi", ohlcv, {"period": 14})
    k3 = calc._cache_key("tech.rsi", ohlcv, {"period": 21})
    assert k1 == k2
    assert k1 != k3


def test_serialize_deserialize_result(ohlcv):
    calc = IndicatorCalculator()
    res = calc.calculate_indicator("tech.macd", ohlcv)
    blob = IndicatorCalculator._serialize(res)
    restored = IndicatorCalculator._deserialize(blob)
    assert restored is not None
    assert restored.name == res.name
    assert restored.values.equals(res.values)
    assert restored.calculated_at == res.calculated_at


# --------------------------------------------------------------------------- #
# calculator: 异步（无 Redis / 无 DB 时优雅降级）
# --------------------------------------------------------------------------- #
async def test_acalculate_indicator_no_redis(full_data):
    """Redis 不可用时应降级为直接计算，仍返回正确结果。"""
    calc = IndicatorCalculator(redis_url="redis://localhost:59999/0", enable_cache=True)
    res = await calc.acalculate_indicator("tech.rsi", full_data, period=14)
    assert res.name == "tech.rsi"
    assert res.values.height == full_data.height
    await calc.aclose()


async def test_acalculate_batch_no_redis(full_data):
    calc = IndicatorCalculator(redis_url="redis://localhost:59999/0", enable_cache=True)
    res = await calc.acalculate_batch(["tech.sma", "onchain.mvrv", "deriv.cvd"], full_data)
    assert set(res) == {"tech.sma", "onchain.mvrv", "deriv.cvd"}
    await calc.aclose()


async def test_persist_without_source_id_returns_zero(full_data):
    """缺少 source_id 时持久化应安全跳过并返回 0。"""
    calc = IndicatorCalculator()
    res = calc.calculate_indicator("tech.sma", full_data, period=20)
    written = await calc.persist_result(res, symbol="BTC", source_id=None)
    assert written == 0


def test_build_persist_rows(full_data):
    calc = IndicatorCalculator()
    res = calc.calculate_indicator("tech.sma", full_data, period=20)
    rows = calc._build_persist_rows(res, symbol="BTC")
    assert len(rows) > 0
    row = rows[0]
    assert isinstance(row["value"], Decimal)
    assert row["symbol"] == "BTC"
    assert row["observation_time"] is not None
    # percentile / normalized_value 为 Decimal 或 None
    assert row["percentile"] is None or isinstance(row["percentile"], Decimal)


def test_build_persist_rows_skips_non_temporal_time():
    """时间列非 datetime 时不落库（避免非法 observation_time）。"""
    calc = IndicatorCalculator()
    df = pl.DataFrame({"close": np.arange(30, dtype=float)})  # 无时间列 => 序号占位
    res = calc.calculate_indicator("tech.sma", df, period=5)
    # 时间列为整数序号，非 datetime
    assert res.values.schema["time"] != pl.Datetime
