"""backtest.data_feed Point-in-Time 防未来数据泄漏测试（最高优先级约束）。"""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import pytest

from app.backtest.data_feed import DataAccessException, PointInTimeDataFeed


def _utc(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


def _prices() -> pl.DataFrame:
    return pl.DataFrame({
        "date": [_utc(2023, 1, 1), _utc(2023, 1, 2), _utc(2023, 1, 3)],
        "close": [100.0, 110.0, 120.0],
    })


async def test_get_price_capped_at_as_of() -> None:
    """请求未来某日价格时被 as_of 游标截断，看不到未来。"""
    feed = PointInTimeDataFeed(current_time=_utc(2023, 1, 2), prices_df=_prices())
    price = await feed.get_price("BTCUSDT", _utc(2023, 1, 3))   # 请求 day3
    assert price is not None
    assert float(price) == 110.0        # 实际只能看到 as_of=day2 的价格


async def test_advance_time_reveals_more() -> None:
    """推进模拟时间后可见更晚的数据。"""
    feed = PointInTimeDataFeed(current_time=_utc(2023, 1, 2), prices_df=_prices())
    feed.advance_time(_utc(2023, 1, 3))
    price = await feed.get_price("BTCUSDT", _utc(2023, 1, 3))
    assert price is not None
    assert float(price) == 120.0


async def test_time_cannot_go_backwards() -> None:
    """模拟时钟单调：回退抛 ValueError。"""
    feed = PointInTimeDataFeed(current_time=_utc(2023, 1, 3), prices_df=_prices())
    with pytest.raises(ValueError):
        feed.advance_time(_utc(2023, 1, 1))


async def test_macro_uses_release_date_not_observation_date() -> None:
    """宏观数据防泄漏核心：用 release_date 过滤，而非 observation_date。

    2022-06 CPI 于 2022-07-13 发布，as_of=2022-07-01 时不可见。
    """
    macro_df = pl.DataFrame({
        "series_id": ["CPI"],
        "observation_date": [date(2022, 6, 1)],
        "release_date": [date(2022, 7, 13)],
        "value": [3.0],
    })
    feed = PointInTimeDataFeed(current_time=_utc(2022, 7, 1), macro_df=macro_df)
    # 发布日之前：不可见
    assert await feed.get_macro_data("CPI", _utc(2022, 7, 1)) is None
    # 发布日之后：可见
    feed.advance_time(_utc(2022, 7, 15))
    val = await feed.get_macro_data("CPI", _utc(2022, 7, 15))
    assert val == 3.0


async def test_indicator_point_in_time() -> None:
    """指标查询同样受 as_of 截断。"""
    ind = pl.DataFrame({
        "date": [_utc(2023, 1, 1), _utc(2023, 1, 2), _utc(2023, 1, 3)],
        "value": [1.0, 2.0, 3.0],
    })
    feed = PointInTimeDataFeed(
        current_time=_utc(2023, 1, 2), indicators={"onchain.mvrv": ind}
    )
    val = await feed.get_indicator("onchain.mvrv", _utc(2023, 1, 3))
    assert val == 2.0       # 只看到 as_of=day2 的值


async def test_validate_no_leakage_passes_for_pit_access() -> None:
    """全部访问均 <= as_of 时，前视校验通过。"""
    feed = PointInTimeDataFeed(current_time=_utc(2023, 1, 1), prices_df=_prices())
    for day in (1, 2, 3):
        feed.advance_time(_utc(2023, 1, day))
        await feed.get_price("BTCUSDT", _utc(2023, 1, day))
    assert feed.validate_no_leakage() is True


def test_data_access_exception_detects_violation() -> None:
    """审计记录：observation_time 或 fetch_time 超过 as_of 即构成泄漏。"""
    ok = DataAccessException(
        source="candles", observation_time=_utc(2023, 1, 1),
        fetch_time=_utc(2023, 1, 1), as_of=_utc(2023, 1, 2),
    )
    assert ok.is_violation is False
    bad_obs = DataAccessException(
        source="candles", observation_time=_utc(2023, 1, 5),
        fetch_time=_utc(2023, 1, 1), as_of=_utc(2023, 1, 2),
    )
    assert bad_obs.is_violation is True
    bad_fetch = DataAccessException(
        source="macro", observation_time=_utc(2023, 1, 1),
        fetch_time=_utc(2023, 1, 9), as_of=_utc(2023, 1, 2),
    )
    assert bad_fetch.is_violation is True
