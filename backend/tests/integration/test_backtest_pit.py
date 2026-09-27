"""Point-in-Time 回测数据供给补充集成测试（防未来数据泄漏 —— 最高优先级约束）。

补充 tests/backtest/test_data_feed_pit.py 未覆盖的场景（全部内存模式，无 DB）：
- fetch_time 双重过滤（observation_time <= as_of AND fetch_time <= as_of，§2.1）；
- fetch_time 为空的行视为立即可用；
- 宏观三时间轴修订（release_date <= as_of + revision_number 版本切换，§2.2）；
- 宏观"观测日早/发布日晚"语义（observation_date 新但未发布 -> 不可见）；
- indicator percentile（as-run 落库分位优先，§2.3）与 engine state 内存 PIT；
- load_bars 内存区间加载；
- 数据缺失返回 None；naive/aware 混用；
- validate_no_leakage 全局断言（max(fetch_time) <= as_of_max）与违规抛错（§2.5）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import pytest

from app.backtest.data_feed import (
    DataAccessException,
    LookAheadViolationError,
    PointInTimeDataFeed,
)


def _utc(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


# ===========================================================================
# fetch_time 双重过滤（§2.1：observation_time <= as_of AND fetch_time <= as_of）
# ===========================================================================
class TestFetchTimeDoubleFilter:
    def _feed(self, as_of: datetime, rows: dict) -> PointInTimeDataFeed:
        return PointInTimeDataFeed(current_time=as_of, prices_df=pl.DataFrame(rows))

    async def test_observed_but_not_yet_fetched_is_invisible(self):
        """数据属于 1月3，但系统 1月10 才抓到 -> as_of=1月5 不可见。"""
        df = pl.DataFrame({
            "time": [_utc(2023, 1, 2), _utc(2023, 1, 3)],
            "close": [110.0, 120.0],
            "fetch_time": [_utc(2023, 1, 2, 1), _utc(2023, 1, 10, 0)],
        })
        feed = self._feed(_utc(2023, 1, 5), df)
        # 1月3 行被 fetch_time 过滤，只能看到 1月2 的 110
        assert float(await feed.get_price("BTCUSDT", _utc(2023, 1, 3))) == 110.0

    async def test_row_becomes_visible_after_fetch_time(self):
        """推进 as_of 越过 fetch_time 后，查询新时点可见该行。

        注：get_price(t) 返回「t 时点可见的最新价」，1月10 才抓到的
        day3 数据在查询 t=1月3 时不可见（那时它尚未到达），
        查询 t=1月10 时成为最新可见价 —— 双重过滤语义正确。
        """
        df = pl.DataFrame({
            "time": [_utc(2023, 1, 2), _utc(2023, 1, 3)],
            "close": [110.0, 120.0],
            "fetch_time": [_utc(2023, 1, 2, 1), _utc(2023, 1, 10, 0)],
        })
        feed = self._feed(_utc(2023, 1, 9), df)
        # 1月9 时点：day3 数据未抓到，最新可见价为 day2 的 110
        assert float(await feed.get_price("BTCUSDT", _utc(2023, 1, 9))) == 110.0
        feed.advance_time(_utc(2023, 1, 10, 12))
        # 1月10 时点：day3 已抓到，成为最新可见价
        assert float(await feed.get_price("BTCUSDT", _utc(2023, 1, 10))) == 120.0

    async def test_future_observation_with_future_fetch_invisible(self):
        """observation 与 fetch 均在未来 -> 不可见（回到前一可见行）。"""
        df = pl.DataFrame({
            "time": [_utc(2023, 1, 1), _utc(2023, 1, 2), _utc(2023, 1, 3), _utc(2023, 1, 4)],
            "close": [100.0, 110.0, 120.0, 130.0],
            "fetch_time": [
                _utc(2023, 1, 1, 1), _utc(2023, 1, 2, 1),
                _utc(2023, 1, 3, 1), _utc(2023, 1, 4, 1),
            ],
        })
        feed = self._feed(_utc(2023, 1, 3, 12), df)
        # 请求 day4：obs 截断到 as_of=day3 12:00 -> 最新可见为 day3 的 120
        assert float(await feed.get_price("BTCUSDT", _utc(2023, 1, 4))) == 120.0

    async def test_null_fetch_time_treated_as_available(self):
        """fetch_time 为 null 视为立即可用（历史导入数据）。"""
        df = pl.DataFrame({
            "time": [_utc(2023, 1, 2), _utc(2023, 1, 3)],
            "close": [110.0, 120.0],
            "fetch_time": [None, None],
        })
        feed = self._feed(_utc(2023, 1, 3), df)
        assert float(await feed.get_price("BTCUSDT", _utc(2023, 1, 3))) == 120.0

    async def test_all_rows_unfetched_returns_none(self):
        """所有行 fetch_time 均在 as_of 之后 -> None（绝不返回未来数据）。"""
        df = pl.DataFrame({
            "time": [_utc(2023, 1, 2), _utc(2023, 1, 3)],
            "close": [110.0, 120.0],
            "fetch_time": [_utc(2023, 1, 20, 0), _utc(2023, 1, 21, 0)],
        })
        feed = self._feed(_utc(2023, 1, 5), df)
        assert await feed.get_price("BTCUSDT", _utc(2023, 1, 3)) is None

    async def test_indicator_fetch_time_filter(self):
        """指标查询同样受 fetch_time 双重过滤。"""
        ind = pl.DataFrame({
            "date": [_utc(2023, 1, 1), _utc(2023, 1, 2)],
            "value": [1.0, 2.0],
            "fetch_time": [_utc(2023, 1, 1, 1), _utc(2023, 1, 9, 0)],
        })
        feed = PointInTimeDataFeed(
            current_time=_utc(2023, 1, 5), indicators={"onchain.mvrv": ind}
        )
        # 1月5 时点：day2 值 1月9 才算出，只能看到 day1 的 1.0
        assert await feed.get_indicator("onchain.mvrv", _utc(2023, 1, 5)) == 1.0
        feed.advance_time(_utc(2023, 1, 9, 1))
        assert await feed.get_indicator("onchain.mvrv", _utc(2023, 1, 9)) == 2.0


# ===========================================================================
# 宏观三时间轴：release_date + revision_number（§2.2）
# ===========================================================================
class TestMacroRevisions:
    def _macro_df(self) -> pl.DataFrame:
        """CPI 2022-06：初版 7-13 发布，修订版 8-10 发布。"""
        return pl.DataFrame({
            "series_id": ["CPI", "CPI"],
            "observation_date": [date(2022, 6, 1), date(2022, 6, 1)],
            "release_date": [date(2022, 7, 13), date(2022, 8, 10)],
            "value": [3.0, 3.2],
            "revision_number": [1, 2],
        })

    async def test_initial_release_visible_after_release_date(self):
        feed = PointInTimeDataFeed(current_time=_utc(2022, 7, 15), macro_df=self._macro_df())
        assert await feed.get_macro_data("CPI", _utc(2022, 7, 15)) == 3.0

    async def test_release_day_boundary_inclusive(self):
        """release_date <= as_of 含边界：发布当天即可见。"""
        feed = PointInTimeDataFeed(current_time=_utc(2022, 7, 13), macro_df=self._macro_df())
        assert await feed.get_macro_data("CPI", _utc(2022, 7, 13)) == 3.0

    async def test_revision_switches_after_later_release(self):
        """修订版在 8-10 发布后取代初版（as-run 版本）。"""
        feed = PointInTimeDataFeed(current_time=_utc(2022, 8, 1), macro_df=self._macro_df())
        assert await feed.get_macro_data("CPI", _utc(2022, 8, 1)) == 3.0  # 修订未发布
        feed.advance_time(_utc(2022, 8, 10, 12))
        assert await feed.get_macro_data("CPI", _utc(2022, 8, 10)) == 3.2  # 修订可见

    async def test_new_observation_not_yet_released_invisible(self):
        """观测日更新但发布日在未来 -> 不可见（禁止用未发布数据模拟过去）。"""
        macro_df = pl.DataFrame({
            "series_id": ["CPI"],
            "observation_date": [date(2022, 7, 1)],
            "release_date": [date(2022, 8, 10)],
            "value": [3.5],
        })
        feed = PointInTimeDataFeed(current_time=_utc(2022, 7, 20), macro_df=macro_df)
        assert await feed.get_macro_data("CPI", _utc(2022, 7, 20)) is None

    async def test_unknown_series_returns_none(self):
        feed = PointInTimeDataFeed(current_time=_utc(2022, 8, 1), macro_df=self._macro_df())
        assert await feed.get_macro_data("UNKNOWN_SERIES", _utc(2022, 8, 1)) is None

    async def test_no_revision_column_still_pit_safe(self):
        """无 revision_number 列时仍按 release_date 过滤。"""
        macro_df = pl.DataFrame({
            "series_id": ["CPI", "CPI"],
            "observation_date": [date(2022, 6, 1), date(2022, 7, 1)],
            "release_date": [date(2022, 7, 13), date(2022, 8, 10)],
            "value": [3.0, 3.5],
        })
        feed = PointInTimeDataFeed(current_time=_utc(2022, 7, 20), macro_df=macro_df)
        assert await feed.get_macro_data("CPI", _utc(2022, 7, 20)) == 3.0


# ===========================================================================
# 指标分位与引擎状态（as-run 优先，§2.3 / §5.1）
# ===========================================================================
class TestPercentileAndEngineState:
    async def test_indicator_percentile_pit(self):
        ind = pl.DataFrame({
            "date": [_utc(2023, 1, 1), _utc(2023, 1, 2), _utc(2023, 1, 3)],
            "value": [1.0, 2.0, 3.0],
            "percentile": [50.0, 80.0, 95.0],
        })
        feed = PointInTimeDataFeed(
            current_time=_utc(2023, 1, 2), indicators={"onchain.mvrv": ind}
        )
        assert await feed.get_indicator_percentile("onchain.mvrv", _utc(2023, 1, 3)) == 80.0
        feed.advance_time(_utc(2023, 1, 3))
        assert await feed.get_indicator_percentile("onchain.mvrv", _utc(2023, 1, 3)) == 95.0

    async def test_percentile_missing_column_returns_none(self):
        ind = pl.DataFrame({
            "date": [_utc(2023, 1, 1)], "value": [1.0],
        })
        feed = PointInTimeDataFeed(
            current_time=_utc(2023, 1, 2), indicators={"onchain.mvrv": ind}
        )
        assert await feed.get_indicator_percentile("onchain.mvrv", _utc(2023, 1, 2)) is None

    async def test_unknown_indicator_returns_none(self):
        feed = PointInTimeDataFeed(
            current_time=_utc(2023, 1, 2),
            indicators={"onchain.mvrv": pl.DataFrame({
                "date": [_utc(2023, 1, 1)], "value": [1.0],
            })},
        )
        assert await feed.get_indicator("tech.rsi", _utc(2023, 1, 2)) is None

    async def test_engine_state_pit_and_clean_dict(self):
        states = pl.DataFrame({
            "date": [_utc(2023, 1, 1), _utc(2023, 1, 2)],
            "cycle_phase": ["RECOVERY", "UPTREND"],
            "fetch_time": [_utc(2023, 1, 1, 1), _utc(2023, 1, 2, 0)],
        })
        feed = PointInTimeDataFeed(current_time=_utc(2023, 1, 2), states_df=states)
        state = await feed.get_engine_state(_utc(2023, 1, 2))
        assert state["cycle_phase"] == "UPTREND"
        # 不泄漏内部辅助列
        assert not any(k.startswith("_") for k in state)

    async def test_engine_state_before_any_state_empty(self):
        states = pl.DataFrame({
            "date": [_utc(2023, 1, 5)],
            "cycle_phase": ["UPTREND"],
        })
        feed = PointInTimeDataFeed(current_time=_utc(2023, 1, 2), states_df=states)
        assert await feed.get_engine_state(_utc(2023, 1, 2)) == {}


# ===========================================================================
# load_bars 与数据缺失
# ===========================================================================
class TestLoadBarsAndMissingData:
    def _prices(self) -> pl.DataFrame:
        return pl.DataFrame({
            "time": [_utc(2023, 1, 1), _utc(2023, 1, 2), _utc(2023, 1, 3), _utc(2023, 1, 4)],
            "close": [100.0, 110.0, 120.0, 130.0],
            "symbol": ["BTCUSDT"] * 4,
        })

    async def test_load_bars_range_and_order(self):
        feed = PointInTimeDataFeed(current_time=_utc(2023, 1, 4), prices_df=self._prices())
        bars = await feed.load_bars("BTCUSDT", _utc(2023, 1, 2), _utc(2023, 1, 3))
        assert bars.height == 2
        assert bars["close"].to_list() == [110.0, 120.0]

    async def test_load_bars_unsorted_input_sorted_output(self):
        df = pl.DataFrame({
            "time": [_utc(2023, 1, 3), _utc(2023, 1, 1), _utc(2023, 1, 2)],
            "close": [120.0, 100.0, 110.0],
        })
        feed = PointInTimeDataFeed(current_time=_utc(2023, 1, 3), prices_df=df)
        bars = await feed.load_bars("BTCUSDT", _utc(2023, 1, 1), _utc(2023, 1, 3))
        assert bars["close"].to_list() == [100.0, 110.0, 120.0]

    async def test_price_unknown_symbol_none(self):
        feed = PointInTimeDataFeed(current_time=_utc(2023, 1, 4), prices_df=self._prices())
        assert await feed.get_price("ETHUSDT", _utc(2023, 1, 3)) is None

    async def test_price_before_first_data_none(self):
        feed = PointInTimeDataFeed(current_time=_utc(2022, 12, 31), prices_df=self._prices())
        assert await feed.get_price("BTCUSDT", _utc(2022, 12, 31)) is None

    async def test_naive_datetime_treated_as_utc(self):
        """naive 时间视为 UTC（与 aware 混用不崩溃）。"""
        feed = PointInTimeDataFeed(current_time=datetime(2023, 1, 2), prices_df=self._prices())
        price = await feed.get_price("BTCUSDT", datetime(2023, 1, 2))
        assert float(price) == 110.0


# ===========================================================================
# 时间旅行断言（§2.5：全局 max(fetch_time) <= as_of_max）
# ===========================================================================
class TestGlobalLeakageAssertion:
    async def test_clean_run_passes(self):
        prices = pl.DataFrame({
            "time": [_utc(2023, 1, 1), _utc(2023, 1, 2)],
            "close": [100.0, 110.0],
            "fetch_time": [_utc(2023, 1, 1, 1), _utc(2023, 1, 2, 1)],
        })
        feed = PointInTimeDataFeed(current_time=_utc(2023, 1, 1), prices_df=prices)
        for day in (1, 2):
            feed.advance_time(_utc(2023, 1, day))
            await feed.get_price("BTCUSDT", _utc(2023, 1, day))
        assert feed.validate_no_leakage() is True

    async def test_violation_record_raises(self):
        feed = PointInTimeDataFeed(current_time=_utc(2023, 1, 1))
        bad = [
            DataAccessException(
                source="candles",
                observation_time=_utc(2023, 1, 5),
                fetch_time=_utc(2023, 1, 1),
                as_of=_utc(2023, 1, 1),
            )
        ]
        with pytest.raises(LookAheadViolationError):
            feed.validate_no_leakage(bad)

    async def test_global_fetch_time_exceeds_as_of_max_raises(self):
        """单条记录自洽，但 max(fetch_time) > as_of_max（游标未推进到该时间）-> 抛错。"""
        feed = PointInTimeDataFeed(current_time=_utc(2023, 1, 1))  # as_of_max = 1月1
        records = [
            DataAccessException(
                source="candles",
                observation_time=_utc(2023, 1, 2),
                fetch_time=_utc(2023, 1, 2),
                as_of=_utc(2023, 1, 2),  # 单条自洽
            )
        ]
        with pytest.raises(LookAheadViolationError, match="全局前视断言"):
            feed.validate_no_leakage(records)

    async def test_external_records_do_not_affect_internal_log(self):
        """校验外部记录列表不影响内部审计日志。"""
        feed = PointInTimeDataFeed(current_time=_utc(2023, 1, 1))
        assert feed.validate_no_leakage([]) is True
        assert feed.access_log == []


# ===========================================================================
# 访问审计日志
# ===========================================================================
class TestAccessAudit:
    async def test_price_access_recorded(self):
        prices = pl.DataFrame({
            "time": [_utc(2023, 1, 1), _utc(2023, 1, 2)],
            "close": [100.0, 110.0],
            "fetch_time": [_utc(2023, 1, 1, 1), _utc(2023, 1, 2, 1)],
        })
        feed = PointInTimeDataFeed(current_time=_utc(2023, 1, 2), prices_df=prices)
        await feed.get_price("BTCUSDT", _utc(2023, 1, 2))

        log = feed.access_log
        assert len(log) == 1
        assert log[0].source == "market_prices"
        assert log[0].is_violation is False

    async def test_macro_access_recorded_with_release_semantics(self):
        macro_df = pl.DataFrame({
            "series_id": ["CPI"],
            "observation_date": [date(2022, 6, 1)],
            "release_date": [date(2022, 7, 13)],
            "value": [3.0],
        })
        feed = PointInTimeDataFeed(current_time=_utc(2022, 7, 15), macro_df=macro_df)
        await feed.get_macro_data("CPI", _utc(2022, 7, 15))

        log = feed.access_log
        assert len(log) == 1
        assert log[0].source == "macro_series"
        assert log[0].observation_time == datetime(2022, 6, 1, tzinfo=UTC)
        assert log[0].fetch_time == datetime(2022, 7, 13, tzinfo=UTC)
