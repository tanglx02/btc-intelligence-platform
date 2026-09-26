"""KrakenProvider — Kraken 现货市场行情 Provider（优先级 5）。

API 文档：https://docs.kraken.com/rest/

数据覆盖：价格 / K线 / 24h统计 / 订单簿 / 成交 / 价差 / CVD / 市值 / ATH
不支持：资金费率 / 未平仓量 / 强平（Kraken 现货 REST 无衍生品公共接口）

注意事项：
- Kraken 使用 "XBT" 表示 BTC（内部 "BTC/USD" 自动转换为 "XBTUSD"）
- 响应格式：{"error": [...], "result": {...}}，error 非空视为 API 错误
- OHLC 接口单次最多返回 720 根，interval 单位为分钟
- Ticker 响应中 result 的 key 为规范化 pair（如 "XXBTZUSD"），需动态解析
"""

from datetime import datetime, timedelta

from loguru import logger

from app.providers.base.config import ProviderConfig
from app.providers.base.market_provider import BaseMarketProvider
from app.providers.base.types import (
    ErrorType,
    FetchResult,
    ProviderMetadata,
    QualityStatus,
)
from app.providers.market._helpers import (
    BTC_CIRCULATING_SUPPLY,
    data_error_result,
    datetime_to_sec,
    empty_result,
    interval_seconds,
    parse_error_result,
    safe_float,
    safe_int,
    sec_to_datetime,
    success_result,
    utcnow,
    validate_candles,
    window_start,
)
from app.providers.market.symbol_mapper import (
    normalize_symbol,
    to_kraken,
)
from app.providers.market.types import (
    ATHData,
    Candle,
    CVDData,
    MarketCapData,
    OrderBookData,
    PriceData,
    SpreadData,
    Trade,
    VolumeData,
)

# 内部标准间隔 -> Kraken interval（分钟）
KRAKEN_INTERVALS: dict[str, int] = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "4h": 240, "1d": 1440, "1w": 10080,
}

# OHLC 接口单次最大返回条数
_MAX_CANDLES = 720
# 批量拉取最大分页数
_MAX_PAGES = 400

# Kraken BTC/USD 数据覆盖起点（XBTZUSD 上线）
_KRAKEN_COVERAGE_START = datetime(2011, 8, 18)

# 现货 Provider 不支持衍生品数据的统一错误说明
_UNSUPPORTED_DERIVATIVE_MSG = (
    "Kraken 现货 REST API 不提供该衍生品数据（请使用 derivatives 类别 Provider）"
)


class KrakenProvider(BaseMarketProvider):
    """Kraken 市场行情 Provider。"""

    def __init__(self, config: ProviderConfig):
        super().__init__(config)

    # ---- BaseProvider 抽象方法 ----

    async def health_check(self) -> bool:
        """轻量级连通性探测：GET /public/Time。"""
        result = await self._request("GET", "/public/Time")
        if not result.success:
            logger.warning(f"[{self.name}] Health check failed: {result.error}")
            return False
        return self._check_api_error(result) is None

    def get_metadata(self) -> ProviderMetadata:
        """返回 Provider 元信息。"""
        return ProviderMetadata(
            name=self.name,
            category=self.category,
            description=self._config.description or "Kraken 现货行情",
            supported_symbols=self._config.supported_symbols or ["BTC/USD"],
            supported_intervals=self._config.supported_intervals
            or ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"],
            data_coverage_start=_KRAKEN_COVERAGE_START,
            documentation_url="https://docs.kraken.com/rest/",
        )

    def get_supported_data_types(self) -> list[str]:
        """返回支持的数据类型列表。"""
        return [
            "price", "ohlcv", "stats_24h", "volume", "market_cap", "ath",
            "order_book", "trades", "spread", "cvd",
        ]

    # ---- BaseMarketProvider 抽象方法 ----

    async def get_current_price(self, symbol: str = "BTC/USD") -> FetchResult:
        """获取当前价格：GET /public/Ticker?pair=XBTUSD。"""
        ticker = await self._fetch_ticker(symbol)
        if not ticker.success:
            return ticker

        raw = ticker.data["raw"]
        normalized = ticker.data["normalized"]

        # c = [最新成交价, 成交手数]
        price = safe_float(self._first_of(raw.get("c")))
        if price is None or price <= 0:
            return data_error_result(self.name, f"非法价格: {raw.get('c')!r}", raw=raw)

        now = utcnow()
        data = PriceData(
            symbol=normalized, price=price,
            bid=safe_float(self._first_of(raw.get("b"))),
            ask=safe_float(self._first_of(raw.get("a"))),
            timestamp=now, source=self.name,
        )
        logger.debug(f"[{self.name}] get_current_price {normalized} = {price}")
        return success_result(ticker, data, observation_time=now)

    async def get_ohlcv(
        self,
        symbol: str = "BTC/USD",
        interval: str = "1h",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> FetchResult:
        """获取 K 线：GET /public/OHLC?pair=XBTUSD&interval=60。

        Kraken OHLC 忽略结束时间参数，始终返回最近 720 根（或 since 之后的），
        因此未指定 start 时按 limit 反推 since，返回后再按范围过滤。
        """
        pair, error = self._resolve_pair(symbol)
        if error:
            return data_error_result(self.name, error)

        kraken_interval = KRAKEN_INTERVALS.get(interval.lower())
        if kraken_interval is None:
            return data_error_result(
                self.name, f"不支持的K线间隔: {interval}（可用: {sorted(KRAKEN_INTERVALS)}）"
            )

        effective_end = end or utcnow()
        params: dict = {"pair": pair, "interval": kraken_interval}
        if start:
            params["since"] = datetime_to_sec(start)
        else:
            # 反推 since 使窗口覆盖 limit 根
            since_dt = effective_end - timedelta(
                seconds=kraken_interval * 60 * min(limit, _MAX_CANDLES)
            )
            params["since"] = datetime_to_sec(since_dt)

        result = await self._request("GET", "/public/OHLC", params=params)
        if not result.success:
            return result

        api_error = self._check_api_error(result)
        if api_error:
            return api_error

        candles = self._parse_ohlc(result.data.get("result", {}), pair)
        if candles is None:
            return parse_error_result(self.name, "OHLC 响应格式非法", raw=result.data)
        if not candles:
            return empty_result(self.name, "OHLC 返回为空", raw=result.data)

        # 范围过滤 + 截断（保留最近的）
        start_boundary = start or (
            effective_end - timedelta(seconds=kraken_interval * 60 * min(limit, _MAX_CANDLES))
        )
        candles = [c for c in candles if start_boundary <= c.timestamp <= effective_end]
        if len(candles) > limit:
            candles = candles[-limit:]
        if not candles:
            return empty_result(self.name, "时间范围内无K线数据", raw=result.data)

        validation_error = validate_candles(candles)
        if validation_error:
            return data_error_result(self.name, validation_error, raw=result.data)

        logger.debug(f"[{self.name}] get_ohlcv {pair} {interval}: {len(candles)} candles")
        return success_result(
            result, candles,
            observation_time=candles[-1].timestamp,
            metadata={"count": len(candles), "interval": interval},
        )

    async def get_24h_stats(self, symbol: str = "BTC/USD") -> FetchResult:
        """获取 24h 统计（从 /public/Ticker 响应中提取 24h 滚动字段）。"""
        ticker = await self._fetch_ticker(symbol)
        if not ticker.success:
            return ticker

        raw = ticker.data["raw"]
        normalized = ticker.data["normalized"]

        # Kraken 滚动字段格式：[今日至今, 最近24小时]
        last = safe_float(self._first_of(raw.get("c")))
        open_24h = safe_float(self._second_of(raw.get("o")))
        high = safe_float(self._second_of(raw.get("h")))
        low = safe_float(self._second_of(raw.get("l")))
        volume = safe_float(self._second_of(raw.get("v")))
        vwap = safe_float(self._second_of(raw.get("p")))
        if last is None or open_24h is None or high is None or low is None or volume is None:
            return data_error_result(self.name, "ticker 24h 字段缺失", raw=raw)

        stats = {
            "symbol": normalized,
            "last_price": last,
            "price_change": last - open_24h,
            "price_change_percent": (
                (last - open_24h) / open_24h * 100.0 if open_24h else None
            ),
            "weighted_avg_price": vwap,
            "high": high,
            "low": low,
            "open": open_24h,
            "volume_base": volume,
            "volume_quote": volume * vwap if vwap else None,
            "trades": safe_int(self._second_of(raw.get("t"))),
            "close_time": utcnow(),
        }
        return success_result(ticker, stats, observation_time=utcnow())

    async def get_volume(self, symbol: str = "BTC/USD", interval: str = "24h") -> FetchResult:
        """获取成交量（基于 Ticker 的 24h 滚动字段）。"""
        ticker = await self._fetch_ticker(symbol)
        if not ticker.success:
            return ticker

        raw = ticker.data["raw"]
        base_volume = safe_float(self._second_of(raw.get("v")))
        if base_volume is None:
            return data_error_result(self.name, "volume 字段缺失", raw=raw)

        vwap = safe_float(self._second_of(raw.get("p")))
        data = VolumeData(
            symbol=ticker.data["normalized"],
            base_volume=base_volume,
            quote_volume=base_volume * vwap if vwap else None,
            interval="24h",
            timestamp=utcnow(),
            source=self.name,
        )
        return success_result(
            ticker, data,
            observation_time=data.timestamp,
            quality_status=QualityStatus.VERIFIED if interval == "24h" else QualityStatus.ESTIMATED,
        )

    async def get_market_cap(self, symbol: str = "BTC") -> FetchResult:
        """获取市值（price × 流通量估算，标记 ESTIMATED）。"""
        asset = symbol.strip().upper().split("/")[0]
        if asset == "XBT":
            asset = "BTC"

        price_result = await self.get_current_price(f"{asset}/USD")
        if not price_result.success:
            return price_result

        price = price_result.data.price
        now = utcnow()
        data = MarketCapData(
            symbol=asset,
            price=price,
            circulating_supply=BTC_CIRCULATING_SUPPLY,
            market_cap=price * BTC_CIRCULATING_SUPPLY,
            timestamp=now,
            source=self.name,
            is_estimated=True,
        )
        return success_result(
            price_result, data,
            observation_time=now,
            quality_status=QualityStatus.ESTIMATED,
            metadata={"supply_source": "platform_constant"},
        )

    async def get_ath(self, symbol: str = "BTC") -> FetchResult:
        """获取历史最高价（扫描 XBT/USD 上线以来的日K线）。"""
        asset = symbol.strip().upper().split("/")[0]
        if asset == "XBT":
            asset = "BTC"

        batch = await self.get_batch_ohlcv(
            f"{asset}/USD", "1d", _KRAKEN_COVERAGE_START, utcnow() + timedelta(days=1)
        )
        if not batch.success:
            return batch

        candles: list[Candle] = batch.data
        if not candles:
            return empty_result(self.name, "ATH 扫描无K线数据")

        peak = max(candles, key=lambda c: c.high)
        current_price = candles[-1].close
        data = ATHData(
            symbol=asset,
            ath_price=peak.high,
            ath_date=peak.timestamp,
            current_price=current_price,
            drawdown_percent=(
                (current_price - peak.high) / peak.high * 100.0 if peak.high > 0 else None
            ),
            source=self.name,
        )
        return success_result(
            batch, data,
            observation_time=utcnow(),
            quality_status=QualityStatus.ESTIMATED,
            metadata={
                "scan_start": _KRAKEN_COVERAGE_START.isoformat(),
                "scanned_candles": len(candles),
            },
        )

    async def get_order_book(self, symbol: str = "BTC/USD", depth: int = 20) -> FetchResult:
        """获取订单簿：GET /public/Depth?pair=XBTUSD&count=depth。"""
        pair, error = self._resolve_pair(symbol)
        if error:
            return data_error_result(self.name, error)

        result = await self._request(
            "GET", "/public/Depth",
            params={"pair": pair, "count": min(max(depth, 1), 500)},
        )
        if not result.success:
            return result

        api_error = self._check_api_error(result)
        if api_error:
            return api_error

        book = self._extract_result_entry(result.data.get("result", {}), pair)
        if book is None:
            return parse_error_result(self.name, "Depth 响应格式非法", raw=result.data)

        bids = self._parse_book_levels(book.get("bids"), depth)
        asks = self._parse_book_levels(book.get("asks"), depth)
        if bids is None or asks is None:
            return parse_error_result(self.name, "bids/asks 格式非法", raw=result.data)
        if not bids or not asks:
            return empty_result(self.name, "订单簿为空", raw=result.data)

        now = utcnow()
        data = OrderBookData(
            symbol=normalize_symbol(symbol), bids=bids, asks=asks,
            timestamp=now, source=self.name,
        )
        return success_result(result, data, observation_time=now)

    async def get_recent_trades(self, symbol: str = "BTC/USD", limit: int = 50) -> FetchResult:
        """获取最近成交：GET /public/Trades?pair=XBTUSD。"""
        pair, error = self._resolve_pair(symbol)
        if error:
            return data_error_result(self.name, error)

        result = await self._request("GET", "/public/Trades", params={"pair": pair})
        if not result.success:
            return result

        api_error = self._check_api_error(result)
        if api_error:
            return api_error

        entry = self._extract_result_entry(result.data.get("result", {}), pair)
        if entry is None or not isinstance(entry, list):
            return parse_error_result(self.name, "Trades 响应格式非法", raw=result.data)

        trades: list[Trade] = []
        for row in entry[-max(1, min(limit, 1000)):]:
            # 行格式：[price, volume, time(秒), buy/sell, market/limit, miscellaneous]
            if not isinstance(row, list) or len(row) < 4:
                continue
            price = safe_float(row[0])
            qty = safe_float(row[1])
            if price is None or qty is None:
                continue
            side = str(row[3]).lower()
            trades.append(Trade(
                trade_id="",  # Kraken 公共 Trades 不提供成交 ID
                price=price,
                quantity=qty,
                timestamp=sec_to_datetime(row[2]),
                side=side if side in ("buy", "sell") else None,
                is_buyer_maker=(side == "sell") if side in ("buy", "sell") else None,
                quote_quantity=price * qty,
            ))

        trades.reverse()  # 按时间倒序返回（最新在前）
        if not trades:
            return empty_result(self.name, "Trades 返回为空", raw=result.data)
        return success_result(
            result, trades,
            observation_time=trades[0].timestamp,
            metadata={"count": len(trades)},
        )

    async def get_spread(self, symbol: str = "BTC/USD") -> FetchResult:
        """获取买卖价差（基于 Ticker 的 b/a 字段）。"""
        ticker = await self._fetch_ticker(symbol)
        if not ticker.success:
            return ticker

        raw = ticker.data["raw"]
        bid = safe_float(self._first_of(raw.get("b")))
        ask = safe_float(self._first_of(raw.get("a")))
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return data_error_result(self.name, f"非法盘口: bid={bid}, ask={ask}", raw=raw)

        mid = (bid + ask) / 2.0
        now = utcnow()
        data = SpreadData(
            symbol=ticker.data["normalized"], bid=bid, ask=ask,
            spread=ask - bid,
            spread_percent=(ask - bid) / mid * 100.0 if mid > 0 else 0.0,
            mid_price=mid, timestamp=now, source=self.name,
        )
        return success_result(ticker, data, observation_time=now)

    async def get_cvd(self, symbol: str = "BTC/USD", interval: str = "1h") -> FetchResult:
        """获取现货 CVD（聚合最近 1000 笔成交，长窗口为近似值）。"""
        trades_result = await self.get_recent_trades(symbol, limit=1000)
        if not trades_result.success:
            return trades_result

        trades: list[Trade] = trades_result.data
        cutoff = utcnow() - window_start(interval)

        buy_volume = 0.0
        sell_volume = 0.0
        count = 0
        for t in trades:
            if t.timestamp and t.timestamp < cutoff:
                continue
            count += 1
            if t.side == "buy":
                buy_volume += t.quantity
            elif t.side == "sell":
                sell_volume += t.quantity

        data = CVDData(
            symbol=normalize_symbol(symbol),
            cvd=buy_volume - sell_volume,
            buy_volume=buy_volume,
            sell_volume=sell_volume,
            trade_count=count,
            interval=interval,
            start_time=cutoff,
            end_time=utcnow(),
            source=self.name,
        )
        return success_result(
            trades_result, data,
            observation_time=utcnow(),
            quality_status=QualityStatus.ESTIMATED,
            metadata={"note": "基于最近 1000 笔成交聚合，长窗口为近似值"},
        )

    async def get_batch_ohlcv(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> FetchResult:
        """批量获取历史 K 线（使用 since 参数向后分页）。"""
        pair, error = self._resolve_pair(symbol)
        if error:
            return data_error_result(self.name, error)

        kraken_interval = KRAKEN_INTERVALS.get(interval.lower())
        if kraken_interval is None:
            return data_error_result(self.name, f"不支持的K线间隔: {interval}")

        step_seconds = interval_seconds(interval) or 3600
        end_sec = datetime_to_sec(end)
        cursor_sec = datetime_to_sec(start)

        all_candles: list[Candle] = []
        last_result: FetchResult | None = None
        pages = 0

        while cursor_sec < end_sec and pages < _MAX_PAGES:
            result = await self._request(
                "GET", "/public/OHLC",
                params={"pair": pair, "interval": kraken_interval, "since": cursor_sec},
            )
            if not result.success:
                if all_candles:
                    logger.warning(
                        f"[{self.name}] batch_ohlcv 分页中断（已获取 {len(all_candles)} 根），"
                        f"返回部分数据: {result.error}"
                    )
                    break
                return result

            api_error = self._check_api_error(result)
            if api_error:
                if all_candles:
                    break
                return api_error

            last_result = result
            ohlc_result = result.data.get("result", {})
            candles = self._parse_ohlc(ohlc_result, pair)
            if candles is None:
                return parse_error_result(self.name, "OHLC 响应格式非法", raw=result.data)
            if not candles:
                break

            if all_candles and candles[0].timestamp <= all_candles[-1].timestamp:
                candles = candles[1:]
            if not candles:
                break

            all_candles.extend(candles)
            pages += 1

            # Kraken 返回 "last" 字段（适合下次 since 的时间戳）
            last_hint = safe_int(ohlc_result.get("last"))
            next_cursor = (
                last_hint
                if last_hint and last_hint > cursor_sec
                else datetime_to_sec(candles[-1].timestamp) + step_seconds
            )
            if next_cursor <= cursor_sec:
                break
            cursor_sec = next_cursor

            if len(candles) < _MAX_CANDLES - 1:
                break

        if not all_candles:
            return empty_result(self.name, f"批量K线为空: {pair} {interval}")

        all_candles = [c for c in all_candles if datetime_to_sec(c.timestamp) < end_sec]
        validation_error = validate_candles(all_candles)
        if validation_error:
            return data_error_result(self.name, validation_error)

        logger.info(
            f"[{self.name}] batch_ohlcv {pair} {interval}: "
            f"{len(all_candles)} candles in {pages} pages"
        )
        base = last_result or FetchResult(
            success=True, provider_name=self.name, fetch_time=utcnow()
        )
        return success_result(
            base, all_candles,
            observation_time=all_candles[-1].timestamp,
            metadata={"count": len(all_candles), "pages": pages, "interval": interval},
        )

    # ---- 内部辅助 ----

    @staticmethod
    def _resolve_pair(symbol: str) -> tuple[str | None, str | None]:
        """解析内部 symbol 为 Kraken pair（BTC/USD -> XBTUSD）。"""
        try:
            return to_kraken(symbol), None
        except ValueError as e:
            return None, str(e)

    def _check_api_error(self, result: FetchResult) -> FetchResult | None:
        """校验 Kraken error 数组，非空时返回失败 FetchResult，否则返回 None。"""
        raw = result.data
        if not isinstance(raw, dict):
            return parse_error_result(
                self.name, f"期望 dict，得到 {type(raw).__name__}", raw=raw
            )
        errors = raw.get("error") or []
        if isinstance(errors, list) and errors:
            return FetchResult(
                success=False,
                error=f"Kraken API error: {'; '.join(str(e) for e in errors)}",
                error_type=self._classify_kraken_error(errors),
                status_code=result.status_code,
                response_time_ms=result.response_time_ms,
                provider_name=self.name,
                fetch_time=result.fetch_time,
                raw_response=raw,
            )
        return None

    @staticmethod
    def _classify_kraken_error(errors: list) -> ErrorType:
        """根据 Kraken 错误码前缀分类错误类型。"""
        text = " ".join(str(e) for e in errors).lower()
        if "rate limit" in text or "rate:limit" in text:
            return ErrorType.RATE_LIMIT
        if "eapi:invalid" in text or "permission" in text or "token" in text:
            return ErrorType.AUTH_ERROR
        if "unavailable" in text or "maint" in text:
            return ErrorType.SERVER_ERROR
        if "unknown asset" in text or "invalid asset" in text or "parameter" in text:
            return ErrorType.DATA_FORMAT
        return ErrorType.SERVER_ERROR

    @staticmethod
    def _extract_result_entry(result_obj: object, pair: str) -> object:
        """从 Kraken result 字典中提取指定 pair 的数据（key 可能被规范化）。"""
        if not isinstance(result_obj, dict):
            return None
        # 优先精确匹配，其次尝试标准代码（XXBTZUSD），最后取第一个非 "last" 的 key
        if pair in result_obj:
            return result_obj[pair]
        standard = to_kraken(pair, standard_code=True)
        if standard in result_obj:
            return result_obj[standard]
        candidates = [v for k, v in result_obj.items() if k != "last"]
        return candidates[0] if candidates else None

    @staticmethod
    def _first_of(field: object) -> object:
        """Kraken 滚动字段取第一个元素（今日至今值）。"""
        if isinstance(field, list) and field:
            return field[0]
        return field

    @staticmethod
    def _second_of(field: object) -> object:
        """Kraken 滚动字段取第二个元素（最近 24h 值）。"""
        if isinstance(field, list) and len(field) >= 2:
            return field[1]
        return None

    def _parse_ohlc(self, result_obj: object, pair: str) -> list[Candle] | None:
        """解析 /public/OHLC 响应为升序 Candle 列表。

        行格式：[time(秒), open, high, low, close, vwap, volume, count]
        """
        entry = self._extract_result_entry(result_obj, pair)
        if entry is None:
            return None
        if not isinstance(entry, list):
            return None

        now = utcnow()
        candles: list[Candle] = []
        for row in entry:
            if not isinstance(row, list) or len(row) < 7:
                return None
            ts = sec_to_datetime(row[0])
            o, h, low, c = (safe_float(row[i]) for i in (1, 2, 3, 4))
            vol = safe_float(row[6])
            if ts is None or None in (o, h, low, c) or vol is None:
                return None
            vwap = safe_float(row[5])
            candles.append(Candle(
                timestamp=ts, open=o, high=h, low=low, close=c,
                volume=vol,
                quote_volume=vol * vwap if vwap else None,
                trades=safe_int(row[7]) if len(row) > 7 else None,
                vwap=vwap,
                # Kraken 返回的最后一根可能仍在进行中
                is_closed=(ts + timedelta(seconds=1)) < now,
            ))
        candles.sort(key=lambda x: x.timestamp)
        return candles

    @staticmethod
    def _parse_book_levels(levels: object, depth: int) -> list[list[float]] | None:
        """解析订单簿档位 [[price(str), qty(str), timestamp], ...]。"""
        if not isinstance(levels, list):
            return None
        parsed: list[list[float]] = []
        for level in levels[:depth]:
            if not isinstance(level, list) or len(level) < 2:
                return None
            price = safe_float(level[0])
            qty = safe_float(level[1])
            if price is None or qty is None:
                return None
            parsed.append([price, qty])
        return parsed

    async def _fetch_ticker(self, symbol: str) -> FetchResult:
        """获取并校验 Ticker 响应。

        成功时将 result.data 替换为 {"raw": dict, "normalized": str}，
        其余元信息（status_code / response_time_ms 等）透传，
        调用方可直接将该结果传给 success_result。
        """
        pair, error = self._resolve_pair(symbol)
        if error:
            return data_error_result(self.name, error)

        result = await self._request("GET", "/public/Ticker", params={"pair": pair})
        if not result.success:
            return result

        api_error = self._check_api_error(result)
        if api_error:
            return api_error

        entry = self._extract_result_entry(result.data.get("result", {}), pair)
        if not isinstance(entry, dict) or not entry:
            return empty_result(self.name, f"Ticker 返回为空: {pair}", raw=result.data)

        result.data = {"raw": entry, "normalized": normalize_symbol(symbol)}
        return result


__all__ = ["KrakenProvider"]
