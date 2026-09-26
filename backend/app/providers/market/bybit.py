"""BybitProvider — Bybit V5 现货/线性合约市场行情 Provider（优先级 4）。

API 文档：https://bybit-exchange.github.io/docs/v5/intro

数据覆盖：
- 现货（category=spot）：价格 / K线 / 24h统计 / 订单簿 / 成交 / 价差 / CVD / 市值 / ATH
- 线性合约（category=linear）：资金费率 / 未平仓量 / 强平订单

响应格式约定：{"retCode": 0, "retMsg": "OK", "result": {...}}，
retCode != 0 视为 API 错误。
Symbol 格式：BTCUSDT（内部 "BTC/USDT" 自动转换）
时间戳：毫秒（字符串）
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
    align_to_interval,
    data_error_result,
    datetime_to_ms,
    empty_result,
    interval_seconds,
    ms_to_datetime,
    parse_error_result,
    safe_float,
    success_result,
    utcnow,
    validate_candles,
    window_start,
)
from app.providers.market.symbol_mapper import normalize_symbol, to_bybit
from app.providers.market.types import (
    ATHData,
    Candle,
    CVDData,
    FundingData,
    LiquidationOrder,
    MarketCapData,
    OpenInterestData,
    OrderBookData,
    PriceData,
    SpreadData,
    Trade,
    VolumeData,
)

# 内部标准间隔 -> Bybit V5 interval 参数
BYBIT_INTERVALS: dict[str, str] = {
    "1m": "1", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720",
    "1d": "D", "1w": "W",
}

# Bybit BTCUSDT 现货上线时间（ATH 扫描起点）
_BYBIT_COVERAGE_START = datetime(2018, 3, 8)

# 单次 kline 最大条数 / 批量拉取最大分页数
_MAX_LIMIT = 1000
_MAX_PAGES = 300


class BybitProvider(BaseMarketProvider):
    """Bybit 市场行情 Provider（V5 统一 API）。"""

    def __init__(self, config: ProviderConfig):
        super().__init__(config)

    # ---- BaseProvider 抽象方法 ----

    async def health_check(self) -> bool:
        """轻量级连通性探测：GET /v5/market/time。"""
        result = await self._request("GET", "/v5/market/time")
        if not result.success:
            logger.warning(f"[{self.name}] Health check failed: {result.error}")
            return False
        return self._check_ret_code(result) is None

    def get_metadata(self) -> ProviderMetadata:
        """返回 Provider 元信息。"""
        return ProviderMetadata(
            name=self.name,
            category=self.category,
            description=self._config.description or "Bybit 现货行情",
            supported_symbols=self._config.supported_symbols or ["BTC/USDT"],
            supported_intervals=self._config.supported_intervals
            or ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"],
            data_coverage_start=_BYBIT_COVERAGE_START,
            documentation_url="https://bybit-exchange.github.io/docs/v5/intro",
        )

    def get_supported_data_types(self) -> list[str]:
        """返回支持的数据类型列表。"""
        return [
            "price", "ohlcv", "stats_24h", "volume", "market_cap", "ath",
            "order_book", "trades", "spread", "cvd",
            "funding_rate", "open_interest", "liquidations",
        ]

    # ---- BaseMarketProvider 抽象方法 ----

    async def get_current_price(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取当前价格：GET /v5/market/tickers?category=spot&symbol=BTCUSDT。"""
        ticker = await self._fetch_spot_ticker(symbol)
        if not ticker.success:
            return ticker

        raw = ticker.data["raw"]
        normalized = ticker.data["normalized"]

        price = safe_float(raw.get("lastPrice"))
        if price is None or price <= 0:
            return data_error_result(self.name, f"非法价格: {raw.get('lastPrice')!r}", raw=raw)

        now = utcnow()
        data = PriceData(
            symbol=normalized, price=price,
            bid=safe_float(raw.get("bid1Price")),
            ask=safe_float(raw.get("ask1Price")),
            timestamp=now, source=self.name,
        )
        logger.debug(f"[{self.name}] get_current_price {normalized} = {price}")
        return success_result(ticker, data, observation_time=now)

    async def get_ohlcv(
        self,
        symbol: str = "BTC/USDT",
        interval: str = "1h",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> FetchResult:
        """获取 K 线：GET /v5/market/kline?category=spot。"""
        try:
            bybit_symbol = to_bybit(symbol)
        except ValueError as e:
            return data_error_result(self.name, str(e))

        bybit_interval = BYBIT_INTERVALS.get(interval.lower())
        if not bybit_interval:
            return data_error_result(
                self.name, f"不支持的K线间隔: {interval}（可用: {sorted(BYBIT_INTERVALS)}）"
            )

        params: dict = {
            "category": "spot",
            "symbol": bybit_symbol,
            "interval": bybit_interval,
            "limit": max(1, min(limit, _MAX_LIMIT)),
        }
        if start:
            params["start"] = datetime_to_ms(align_to_interval(start, interval))
        if end:
            params["end"] = datetime_to_ms(end)

        result = await self._request("GET", "/v5/market/kline", params=params)
        if not result.success:
            return result

        api_error = self._check_ret_code(result)
        if api_error:
            return api_error

        candles = self._parse_klines(result.data.get("result", {}).get("list", []))
        if not candles:
            return empty_result(self.name, "kline 返回为空", raw=result.data)

        validation_error = validate_candles(candles)
        if validation_error:
            return data_error_result(self.name, validation_error, raw=result.data)

        logger.debug(f"[{self.name}] get_ohlcv {bybit_symbol} {interval}: {len(candles)} candles")
        return success_result(
            result, candles,
            observation_time=candles[-1].timestamp,
            metadata={"count": len(candles), "interval": interval},
        )

    async def get_24h_stats(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取 24h 统计：GET /v5/market/tickers?category=spot。"""
        ticker = await self._fetch_spot_ticker(symbol)
        if not ticker.success:
            return ticker

        raw = ticker.data["raw"]
        normalized = ticker.data["normalized"]

        last = safe_float(raw.get("lastPrice"))
        if last is None:
            return data_error_result(self.name, "lastPrice 缺失", raw=raw)

        price_change_pct = safe_float(raw.get("price24hPcnt"))
        prev_close = safe_float(raw.get("prevPrice24h"))
        # Bybit 不直接提供 24h 开盘价，由涨跌幅反推
        open_24h = (
            last / (1 + price_change_pct)
            if price_change_pct is not None and price_change_pct > -1
            else None
        )

        stats = {
            "symbol": normalized,
            "last_price": last,
            "price_change": (last - open_24h) if open_24h is not None else None,
            "price_change_percent": (
                price_change_pct * 100.0 if price_change_pct is not None else None
            ),
            "weighted_avg_price": None,
            "high": safe_float(raw.get("highPrice24h")),
            "low": safe_float(raw.get("lowPrice24h")),
            "open": open_24h,
            "volume_base": safe_float(raw.get("volume24h")) or 0.0,
            "volume_quote": safe_float(raw.get("turnover24h")) or 0.0,
            "trades": None,
            "close_time": utcnow(),
            "prev_price_24h": prev_close,
        }
        return success_result(ticker, stats, observation_time=utcnow())

    async def get_volume(self, symbol: str = "BTC/USDT", interval: str = "24h") -> FetchResult:
        """获取成交量（基于 spot tickers 24h 数据）。"""
        ticker = await self._fetch_spot_ticker(symbol)
        if not ticker.success:
            return ticker

        raw = ticker.data["raw"]
        base_volume = safe_float(raw.get("volume24h"))
        if base_volume is None:
            return data_error_result(self.name, "volume24h 缺失", raw=raw)

        data = VolumeData(
            symbol=ticker.data["normalized"],
            base_volume=base_volume,
            quote_volume=safe_float(raw.get("turnover24h")),
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

        price_result = await self.get_current_price(f"{asset}/USDT")
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
        """获取历史最高价（扫描 Bybit 数据覆盖起点以来的日K线）。"""
        asset = symbol.strip().upper().split("/")[0]
        if asset == "XBT":
            asset = "BTC"

        batch = await self.get_batch_ohlcv(
            f"{asset}/USDT", "1d", _BYBIT_COVERAGE_START, utcnow() + timedelta(days=1)
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
                "scan_start": _BYBIT_COVERAGE_START.isoformat(),
                "scanned_candles": len(candles),
            },
        )

    async def get_order_book(self, symbol: str = "BTC/USDT", depth: int = 20) -> FetchResult:
        """获取订单簿：GET /v5/market/orderbook?category=spot。"""
        try:
            bybit_symbol = to_bybit(symbol)
        except ValueError as e:
            return data_error_result(self.name, str(e))

        # spot 支持 limit: 1..200（200 档）
        result = await self._request(
            "GET", "/v5/market/orderbook",
            params={
                "category": "spot",
                "symbol": bybit_symbol,
                "limit": min(max(depth, 1), 200),
            },
        )
        if not result.success:
            return result

        api_error = self._check_ret_code(result)
        if api_error:
            return api_error

        payload = result.data.get("result", {})
        bids = self._parse_book_levels(payload.get("b"), depth)
        asks = self._parse_book_levels(payload.get("s"), depth)
        if bids is None or asks is None:
            return parse_error_result(self.name, "b/s 格式非法", raw=result.data)
        if not bids or not asks:
            return empty_result(self.name, "订单簿为空", raw=result.data)

        now = ms_to_datetime(payload.get("ts")) or utcnow()
        data = OrderBookData(
            symbol=normalize_symbol(symbol), bids=bids, asks=asks,
            timestamp=now, source=self.name,
            last_update_id=str(payload.get("u", "")),
        )
        return success_result(result, data, observation_time=now)

    async def get_recent_trades(self, symbol: str = "BTC/USDT", limit: int = 50) -> FetchResult:
        """获取最近成交：GET /v5/market/recent-trade。"""
        try:
            bybit_symbol = to_bybit(symbol)
        except ValueError as e:
            return data_error_result(self.name, str(e))

        result = await self._request(
            "GET", "/v5/market/recent-trade",
            params={
                "category": "spot",
                "symbol": bybit_symbol,
                "limit": max(1, min(limit, 60)),
            },
        )
        if not result.success:
            return result

        api_error = self._check_ret_code(result)
        if api_error:
            return api_error

        trades: list[Trade] = []
        for item in result.data.get("result", {}).get("list", []):
            if not isinstance(item, dict):
                continue
            price = safe_float(item.get("price"))
            qty = safe_float(item.get("size"))
            if price is None or qty is None:
                continue
            side = str(item.get("S", "")).upper()  # B=主动买 / S=主动卖
            trades.append(Trade(
                trade_id=str(item.get("v", "")),
                price=price,
                quantity=qty,
                timestamp=ms_to_datetime(item.get("T")),
                side="buy" if side == "B" else "sell" if side == "S" else None,
                is_buyer_maker=(side == "S") if side in ("B", "S") else None,
                quote_quantity=price * qty,
            ))

        if not trades:
            return empty_result(self.name, "recent-trade 返回为空", raw=result.data)
        return success_result(
            result, trades,
            observation_time=trades[0].timestamp,
            metadata={"count": len(trades)},
        )

    async def get_spread(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取买卖价差（基于 spot tickers 的 bid1/ask1）。"""
        ticker = await self._fetch_spot_ticker(symbol)
        if not ticker.success:
            return ticker

        raw = ticker.data["raw"]
        bid = safe_float(raw.get("bid1Price"))
        ask = safe_float(raw.get("ask1Price"))
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

    async def get_cvd(self, symbol: str = "BTC/USDT", interval: str = "1h") -> FetchResult:
        """获取现货 CVD（聚合最近 60 笔成交，受接口上限约束为粗略近似）。"""
        trades_result = await self.get_recent_trades(symbol, limit=60)
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
            metadata={"note": "基于最近 60 笔成交聚合，仅为粗略近似"},
        )

    async def get_batch_ohlcv(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> FetchResult:
        """批量获取历史 K 线（内部分页覆盖完整时间范围）。"""
        try:
            bybit_symbol = to_bybit(symbol)
        except ValueError as e:
            return data_error_result(self.name, str(e))

        bybit_interval = BYBIT_INTERVALS.get(interval.lower())
        if not bybit_interval:
            return data_error_result(self.name, f"不支持的K线间隔: {interval}")

        step_seconds = interval_seconds(interval) or 3600
        start = align_to_interval(start, interval)
        end_ms = datetime_to_ms(end)
        cursor_ms = datetime_to_ms(start)

        all_candles: list[Candle] = []
        last_result: FetchResult | None = None
        pages = 0

        while cursor_ms < end_ms and pages < _MAX_PAGES:
            result = await self._request(
                "GET", "/v5/market/kline",
                params={
                    "category": "spot",
                    "symbol": bybit_symbol,
                    "interval": bybit_interval,
                    "start": cursor_ms,
                    "end": end_ms,
                    "limit": _MAX_LIMIT,
                },
            )
            if not result.success:
                if all_candles:
                    logger.warning(
                        f"[{self.name}] batch_ohlcv 分页中断（已获取 {len(all_candles)} 根），"
                        f"返回部分数据: {result.error}"
                    )
                    break
                return result

            api_error = self._check_ret_code(result)
            if api_error:
                if all_candles:
                    break
                return api_error

            last_result = result
            candles = self._parse_klines(result.data.get("result", {}).get("list", []))
            if not candles:
                break

            if all_candles and candles[0].timestamp <= all_candles[-1].timestamp:
                candles = candles[1:]
            if not candles:
                break

            all_candles.extend(candles)
            pages += 1
            cursor_ms = datetime_to_ms(candles[-1].timestamp) + step_seconds * 1000

            if len(candles) < _MAX_LIMIT - 1:
                break

        if not all_candles:
            return empty_result(self.name, f"批量K线为空: {bybit_symbol} {interval}")

        all_candles = [c for c in all_candles if datetime_to_ms(c.timestamp) < end_ms]
        validation_error = validate_candles(all_candles)
        if validation_error:
            return data_error_result(self.name, validation_error)

        logger.info(
            f"[{self.name}] batch_ohlcv {bybit_symbol} {interval}: "
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

    # ---- 线性合约扩展方法 ----

    async def get_funding_rate(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取永续资金费率：GET /v5/market/tickers?category=linear。"""
        try:
            bybit_symbol = to_bybit(symbol)
            normalized = normalize_symbol(symbol)
        except ValueError as e:
            return data_error_result(self.name, str(e))

        result = await self._request(
            "GET", "/v5/market/tickers",
            params={"category": "linear", "symbol": bybit_symbol},
        )
        if not result.success:
            return result

        api_error = self._check_ret_code(result)
        if api_error:
            return api_error

        items = result.data.get("result", {}).get("list", [])
        if not items or not isinstance(items[0], dict):
            return empty_result(
                self.name, f"linear tickers 返回为空: {bybit_symbol}", raw=result.data
            )

        raw = items[0]
        funding_rate = safe_float(raw.get("fundingRate"))
        if funding_rate is None:
            return data_error_result(self.name, "fundingRate 缺失", raw=raw)

        data = FundingData(
            symbol=normalized,
            funding_rate=funding_rate,
            mark_price=safe_float(raw.get("markPrice")),
            index_price=safe_float(raw.get("indexPrice")),
            next_funding_time=ms_to_datetime(raw.get("nextFundingTime")),
            timestamp=utcnow(),
            source=self.name,
        )
        return success_result(result, data, observation_time=data.timestamp)

    async def get_open_interest(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取未平仓合约量：GET /v5/market/open-interest?category=linear。"""
        try:
            bybit_symbol = to_bybit(symbol)
            normalized = normalize_symbol(symbol)
        except ValueError as e:
            return data_error_result(self.name, str(e))

        result = await self._request(
            "GET", "/v5/market/open-interest",
            params={
                "category": "linear",
                "symbol": bybit_symbol,
                "intervalTime": "5min",
                "limit": 1,
            },
        )
        if not result.success:
            return result

        api_error = self._check_ret_code(result)
        if api_error:
            return api_error

        payload = result.data.get("result", {})
        items = payload.get("list", [])
        if not items or not isinstance(items[0], dict):
            return empty_result(
                self.name, f"open-interest 返回为空: {bybit_symbol}", raw=result.data
            )

        raw = items[0]
        oi = safe_float(raw.get("openInterest"))
        if oi is None:
            return data_error_result(self.name, "openInterest 缺失", raw=raw)

        timestamp = ms_to_datetime(raw.get("timestamp")) or utcnow()
        data = OpenInterestData(
            symbol=normalized,
            open_interest=oi,
            # Bybit V5 不直接提供名义价值，由 Service 层结合标记价格计算
            open_interest_value=None,
            timestamp=timestamp,
            source=self.name,
        )
        return success_result(result, data, observation_time=timestamp)

    async def get_liquidations(self, symbol: str = "BTC/USDT", limit: int = 100) -> FetchResult:
        """获取强平订单：GET /v5/market/recent-trade?category=linear&tradeType=Liquidation。"""
        try:
            bybit_symbol = to_bybit(symbol)
            normalized = normalize_symbol(symbol)
        except ValueError as e:
            return data_error_result(self.name, str(e))

        result = await self._request(
            "GET", "/v5/market/recent-trade",
            params={
                "category": "linear",
                "symbol": bybit_symbol,
                "tradeType": "Liquidation",
                "limit": max(1, min(limit, 1000)),
            },
        )
        if not result.success:
            return result

        api_error = self._check_ret_code(result)
        if api_error:
            return api_error

        orders: list[LiquidationOrder] = []
        for item in result.data.get("result", {}).get("list", []):
            if not isinstance(item, dict):
                continue
            price = safe_float(item.get("price"))
            qty = safe_float(item.get("size"))
            if price is None or qty is None:
                continue
            side = str(item.get("S", "")).upper()
            orders.append(LiquidationOrder(
                symbol=normalized,
                side="buy" if side == "B" else "sell",
                price=price,
                quantity=qty,
                timestamp=ms_to_datetime(item.get("T")),
            ))

        return success_result(
            result, orders,
            observation_time=utcnow(),
            quality_status=QualityStatus.VERIFIED if orders else QualityStatus.ESTIMATED,
            metadata={"count": len(orders)},
        )

    # ---- 内部辅助 ----

    async def _fetch_spot_ticker(self, symbol: str) -> FetchResult:
        """获取并校验现货 ticker，成功时 data 为 {"raw": dict, "normalized": str}。"""
        try:
            bybit_symbol = to_bybit(symbol)
            normalized = normalize_symbol(symbol)
        except ValueError as e:
            return data_error_result(self.name, str(e))

        result = await self._request(
            "GET", "/v5/market/tickers",
            params={"category": "spot", "symbol": bybit_symbol},
        )
        if not result.success:
            return result

        api_error = self._check_ret_code(result)
        if api_error:
            return api_error

        items = result.data.get("result", {}).get("list", [])
        if not items or not isinstance(items[0], dict):
            return empty_result(
                self.name, f"spot tickers 返回为空: {bybit_symbol}", raw=result.data
            )

        result.data = {"raw": items[0], "normalized": normalized}
        return result

    def _check_ret_code(self, result: FetchResult) -> FetchResult | None:
        """校验 Bybit retCode，非 0 时返回失败 FetchResult，否则返回 None。"""
        raw = result.data
        if not isinstance(raw, dict):
            return parse_error_result(
                self.name, f"期望 dict，得到 {type(raw).__name__}", raw=raw
            )
        ret_code = raw.get("retCode")
        if safe_float(ret_code, default=None) not in (0, 0.0):
            ret_msg = raw.get("retMsg", "")
            code_str = str(ret_code)
            error_type = (
                ErrorType.RATE_LIMIT if code_str in ("10006", "10018")
                else ErrorType.AUTH_ERROR if code_str.startswith(("30", "33"))
                else ErrorType.DATA_FORMAT if code_str.startswith("100")
                else ErrorType.SERVER_ERROR
            )
            return FetchResult(
                success=False,
                error=f"Bybit API error retCode={ret_code}: {ret_msg}",
                error_type=error_type,
                status_code=result.status_code,
                response_time_ms=result.response_time_ms,
                provider_name=self.name,
                fetch_time=result.fetch_time,
                raw_response=raw,
            )
        return None

    @staticmethod
    def _parse_klines(rows: object) -> list[Candle]:
        """解析 Bybit kline（倒序输入 -> 升序输出）。

        字段顺序：[0]start(ms) [1]open [2]high [3]low [4]close
        [5]volume(币) [6]turnover(额) [7]confirm(0=进行中,1=已收盘)
        """
        if not isinstance(rows, list):
            return []

        candles: list[Candle] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            ts = ms_to_datetime(row[0])
            o, h, low, c = (safe_float(row[i]) for i in (1, 2, 3, 4))
            vol = safe_float(row[5])
            if ts is None or None in (o, h, low, c) or vol is None:
                continue
            confirm = row[7] if len(row) > 7 else "1"
            candles.append(Candle(
                timestamp=ts, open=o, high=h, low=low, close=c,
                volume=vol,
                quote_volume=safe_float(row[6]) if len(row) > 6 else None,
                trades=None, is_closed=str(confirm) == "1",
            ))
        candles.reverse()  # Bybit 返回倒序
        return candles

    @staticmethod
    def _parse_book_levels(levels: object, depth: int) -> list[list[float]] | None:
        """解析订单簿档位 [[price, qty], ...]。"""
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


__all__ = ["BybitProvider"]
