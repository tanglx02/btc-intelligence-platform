"""BinanceProvider — Binance 现货/合约市场行情 Provider（优先级 1）。

API 文档：https://binance-docs.github.io/apidocs/spot/en/
合约 API：https://binance-docs.github.io/apidocs/futures/en/

数据覆盖：
- 现货：价格 / K线 / 24h统计 / 订单簿 / 成交 / 价差 / CVD / 市值 / ATH
- 合约（fapi.binance.com，公共接口无需鉴权）：资金费率 / 未平仓量 / 强平订单

Symbol 格式：BTCUSDT（内部 "BTC/USDT" 自动转换）
"""

from datetime import datetime, timedelta

from loguru import logger

from app.providers.base.config import ProviderConfig
from app.providers.base.market_provider import BaseMarketProvider
from app.providers.base.types import (
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
    safe_int,
    success_result,
    utcnow,
    validate_candles,
    window_start,
)
from app.providers.market.symbol_mapper import normalize_symbol, to_binance
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

# 合约 API 独立域名（httpx 支持绝对 URL 覆盖 base_url）
FUTURES_BASE_URL = "https://fapi.binance.com"

# Binance 支持的 K 线间隔（内部标准间隔与 Binance 一致，1M 除外）
BINANCE_INTERVALS: dict[str, str] = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "8h": "8h", "12h": "12h",
    "1d": "1d", "3d": "3d", "1w": "1w",
}

# BTCUSDT 现货上线时间（ATH 扫描起点）
_BTCUSDT_LISTING = datetime(2017, 8, 17)

# 单次 klines 最大条数 / 批量拉取最大分页数
_MAX_LIMIT = 1000
_MAX_PAGES = 200


class BinanceProvider(BaseMarketProvider):
    """Binance 市场行情 Provider。

    现货数据来自 api.binance.com（配置 base_url），
    合约衍生数据（资金费率/未平仓/强平）来自 fapi.binance.com 公共接口。
    """

    def __init__(self, config: ProviderConfig):
        super().__init__(config)

    # ---- BaseProvider 抽象方法 ----

    async def health_check(self) -> bool:
        """轻量级连通性探测：GET /api/v3/ping。"""
        result = await self._request("GET", "/api/v3/ping")
        healthy = result.success
        if not healthy:
            logger.warning(f"[{self.name}] Health check failed: {result.error}")
        return healthy

    def get_metadata(self) -> ProviderMetadata:
        """返回 Provider 元信息。"""
        return ProviderMetadata(
            name=self.name,
            category=self.category,
            description=self._config.description or "Binance 现货行情（全球流动性最佳）",
            supported_symbols=self._config.supported_symbols or ["BTC/USDT"],
            supported_intervals=self._config.supported_intervals
            or ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"],
            data_coverage_start=_BTCUSDT_LISTING,
            documentation_url="https://binance-docs.github.io/apidocs/spot/en/",
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
        """获取当前价格：GET /api/v3/ticker/price。"""
        try:
            binance_symbol = to_binance(symbol)
            normalized = normalize_symbol(symbol)
        except ValueError as e:
            return data_error_result(self.name, str(e))

        result = await self._request(
            "GET", "/api/v3/ticker/price", params={"symbol": binance_symbol}
        )
        if not result.success:
            return result

        raw = result.data
        if not isinstance(raw, dict):
            return parse_error_result(self.name, f"期望 dict，得到 {type(raw).__name__}", raw=raw)

        price = safe_float(raw.get("price"))
        if price is None or price <= 0:
            return data_error_result(self.name, f"非法价格: {raw.get('price')!r}", raw=raw)

        now = utcnow()
        data = PriceData(
            symbol=normalized, price=price, bid=None, ask=None,
            timestamp=now, source=self.name,
        )
        logger.debug(f"[{self.name}] get_current_price {normalized} = {price}")
        return success_result(result, data, observation_time=now)

    async def get_ohlcv(
        self,
        symbol: str = "BTC/USDT",
        interval: str = "1h",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> FetchResult:
        """获取 K 线：GET /api/v3/klines。"""
        try:
            binance_symbol = to_binance(symbol)
        except ValueError as e:
            return data_error_result(self.name, str(e))

        binance_interval = BINANCE_INTERVALS.get(interval.lower())
        if not binance_interval:
            return data_error_result(
                self.name, f"不支持的K线间隔: {interval}（可用: {sorted(BINANCE_INTERVALS)}）"
            )

        params: dict = {
            "symbol": binance_symbol,
            "interval": binance_interval,
            "limit": max(1, min(limit, _MAX_LIMIT)),
        }
        if start:
            params["startTime"] = datetime_to_ms(align_to_interval(start, interval))
        if end:
            params["endTime"] = datetime_to_ms(end)

        result = await self._request("GET", "/api/v3/klines", params=params)
        if not result.success:
            return result

        candles, error = self._parse_klines(result.data)
        if error:
            return parse_error_result(self.name, error, raw=result.data)
        if not candles:
            return empty_result(self.name, "klines 返回为空", raw=result.data)

        validation_error = validate_candles(candles)
        if validation_error:
            return data_error_result(self.name, validation_error, raw=result.data)

        logger.debug(f"[{self.name}] get_ohlcv {binance_symbol} {interval}: {len(candles)} candles")
        return success_result(
            result, candles,
            observation_time=candles[-1].timestamp,
            metadata={"count": len(candles), "interval": interval},
        )

    async def get_24h_stats(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取 24h 统计：GET /api/v3/ticker/24hr。"""
        try:
            binance_symbol = to_binance(symbol)
            normalized = normalize_symbol(symbol)
        except ValueError as e:
            return data_error_result(self.name, str(e))

        result = await self._request(
            "GET", "/api/v3/ticker/24hr", params={"symbol": binance_symbol}
        )
        if not result.success:
            return result

        raw = result.data
        if not isinstance(raw, dict):
            return parse_error_result(self.name, f"期望 dict，得到 {type(raw).__name__}", raw=raw)

        stats = self._parse_24hr(raw, normalized)
        if stats is None:
            return data_error_result(self.name, "24hr 关键字段缺失或非法", raw=raw)

        observation = ms_to_datetime(raw.get("closeTime")) or utcnow()
        return success_result(result, stats, observation_time=observation)

    async def get_volume(self, symbol: str = "BTC/USDT", interval: str = "24h") -> FetchResult:
        """获取成交量（基于 24h 统计）。"""
        stats_result = await self.get_24h_stats(symbol)
        if not stats_result.success:
            return stats_result

        stats = stats_result.data
        data = VolumeData(
            symbol=stats["symbol"],
            base_volume=stats["volume_base"],
            quote_volume=stats["volume_quote"],
            interval="24h",
            timestamp=stats_result.observation_time,
            source=self.name,
        )
        return success_result(
            stats_result, data,
            observation_time=stats_result.observation_time,
            quality_status=QualityStatus.VERIFIED if interval == "24h" else QualityStatus.ESTIMATED,
        )

    async def get_market_cap(self, symbol: str = "BTC") -> FetchResult:
        """获取市值（price × 流通量估算，标记 ESTIMATED）。

        Binance 不提供流通量接口，使用平台常量 BTC_CIRCULATING_SUPPLY 估算。
        """
        asset = symbol.strip().upper().split("/")[0]
        if asset == "XBT":
            asset = "BTC"
        price_symbol = f"{asset}/USDT"

        price_result = await self.get_current_price(price_symbol)
        if not price_result.success:
            return price_result

        price = price_result.data.price
        supply = BTC_CIRCULATING_SUPPLY
        now = utcnow()
        data = MarketCapData(
            symbol=asset,
            price=price,
            circulating_supply=supply,
            market_cap=price * supply,
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
        """获取历史最高价（扫描 BTCUSDT 上线以来的月线数据）。"""
        asset = symbol.strip().upper().split("/")[0]
        if asset == "XBT":
            asset = "BTC"
        pair = f"{asset}/USDT"

        batch = await self.get_batch_ohlcv(
            pair, "1d", _BTCUSDT_LISTING, utcnow() + timedelta(days=1)
        )
        if not batch.success:
            return batch

        candles: list[Candle] = batch.data
        if not candles:
            return empty_result(self.name, "ATH 扫描无K线数据")

        peak = max(candles, key=lambda c: c.high)
        current_price = candles[-1].close
        drawdown = (
            (current_price - peak.high) / peak.high * 100.0 if peak.high > 0 else None
        )

        data = ATHData(
            symbol=asset,
            ath_price=peak.high,
            ath_date=peak.timestamp,
            current_price=current_price,
            drawdown_percent=drawdown,
            source=self.name,
        )
        return success_result(
            batch, data,
            observation_time=utcnow(),
            quality_status=QualityStatus.ESTIMATED,
            metadata={
                "scan_start": _BTCUSDT_LISTING.isoformat(),
                "scanned_candles": len(candles),
            },
        )

    async def get_order_book(self, symbol: str = "BTC/USDT", depth: int = 20) -> FetchResult:
        """获取订单簿：GET /api/v3/depth。"""
        try:
            binance_symbol = to_binance(symbol)
            normalized = normalize_symbol(symbol)
        except ValueError as e:
            return data_error_result(self.name, str(e))

        # Binance 仅支持离散档位
        limit = min(max(depth, 5), 5000)
        allowed = [5, 10, 20, 50, 100, 500, 1000, 5000]
        limit = next((v for v in allowed if v >= limit), 5000)

        result = await self._request(
            "GET", "/api/v3/depth", params={"symbol": binance_symbol, "limit": limit}
        )
        if not result.success:
            return result

        raw = result.data
        if not isinstance(raw, dict):
            return parse_error_result(self.name, f"期望 dict，得到 {type(raw).__name__}", raw=raw)

        bids = self._parse_book_levels(raw.get("bids"), depth)
        asks = self._parse_book_levels(raw.get("asks"), depth)
        if bids is None or asks is None:
            return parse_error_result(self.name, "bids/asks 格式非法", raw=raw)
        if not bids or not asks:
            return empty_result(self.name, "订单簿为空", raw=raw)

        now = utcnow()
        data = OrderBookData(
            symbol=normalized, bids=bids, asks=asks, timestamp=now,
            source=self.name, last_update_id=str(raw.get("lastUpdateId", "")),
        )
        return success_result(result, data, observation_time=now)

    async def get_recent_trades(self, symbol: str = "BTC/USDT", limit: int = 50) -> FetchResult:
        """获取最近成交：GET /api/v3/trades。"""
        try:
            binance_symbol = to_binance(symbol)
        except ValueError as e:
            return data_error_result(self.name, str(e))

        result = await self._request(
            "GET", "/api/v3/trades",
            params={"symbol": binance_symbol, "limit": max(1, min(limit, 1000))},
        )
        if not result.success:
            return result

        raw = result.data
        if not isinstance(raw, list):
            return parse_error_result(self.name, f"期望 list，得到 {type(raw).__name__}", raw=raw)

        trades: list[Trade] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            price = safe_float(item.get("price"))
            qty = safe_float(item.get("qty"))
            if price is None or qty is None:
                continue
            is_buyer_maker = bool(item.get("isBuyerMaker"))
            trades.append(Trade(
                trade_id=str(item.get("id", "")),
                price=price,
                quantity=qty,
                timestamp=ms_to_datetime(item.get("time")),
                # isBuyerMaker=True 表示买方挂单，即 taker 为卖方
                side="sell" if is_buyer_maker else "buy",
                is_buyer_maker=is_buyer_maker,
                quote_quantity=safe_float(item.get("quoteQty")),
            ))

        if not trades:
            return empty_result(self.name, "trades 返回为空", raw=raw)
        return success_result(
            result, trades,
            observation_time=trades[0].timestamp,
            metadata={"count": len(trades)},
        )

    async def get_spread(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取买卖价差：GET /api/v3/ticker/bookTicker。"""
        try:
            binance_symbol = to_binance(symbol)
            normalized = normalize_symbol(symbol)
        except ValueError as e:
            return data_error_result(self.name, str(e))

        result = await self._request(
            "GET", "/api/v3/ticker/bookTicker", params={"symbol": binance_symbol}
        )
        if not result.success:
            return result

        raw = result.data
        if not isinstance(raw, dict):
            return parse_error_result(self.name, f"期望 dict，得到 {type(raw).__name__}", raw=raw)

        bid = safe_float(raw.get("bidPrice"))
        ask = safe_float(raw.get("askPrice"))
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return data_error_result(self.name, f"非法盘口: bid={bid}, ask={ask}", raw=raw)

        mid = (bid + ask) / 2.0
        now = utcnow()
        data = SpreadData(
            symbol=normalized, bid=bid, ask=ask,
            spread=ask - bid,
            spread_percent=(ask - bid) / mid * 100.0 if mid > 0 else 0.0,
            mid_price=mid, timestamp=now, source=self.name,
        )
        return success_result(result, data, observation_time=now)

    async def get_cvd(self, symbol: str = "BTC/USDT", interval: str = "1h") -> FetchResult:
        """获取现货 CVD（聚合最近成交记录，受 trades 接口条数限制为近似值）。"""
        trades_result = await self.get_recent_trades(symbol, limit=1000)
        if not trades_result.success:
            return trades_result

        trades: list[Trade] = trades_result.data
        window = window_start(interval)
        cutoff = utcnow() - window

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
        """批量获取历史 K 线（内部分页覆盖完整时间范围）。"""
        try:
            binance_symbol = to_binance(symbol)
        except ValueError as e:
            return data_error_result(self.name, str(e))

        binance_interval = BINANCE_INTERVALS.get(interval.lower())
        if not binance_interval:
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
                "GET", "/api/v3/klines",
                params={
                    "symbol": binance_symbol,
                    "interval": binance_interval,
                    "startTime": cursor_ms,
                    "endTime": end_ms,
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

            last_result = result
            candles, error = self._parse_klines(result.data)
            if error:
                return parse_error_result(self.name, error, raw=result.data)
            if not candles:
                break

            # 去重衔接（首根可能与上一页末根重叠）
            if all_candles and candles[0].timestamp <= all_candles[-1].timestamp:
                candles = candles[1:]
            if not candles:
                break

            all_candles.extend(candles)
            pages += 1
            cursor_ms = datetime_to_ms(candles[-1].timestamp) + step_seconds * 1000

            # 不足一页说明已覆盖到 endTime
            if len(candles) < _MAX_LIMIT - 1:
                break

        if not all_candles:
            return empty_result(self.name, f"批量K线为空: {binance_symbol} {interval}")

        # 过滤超出范围的数据
        all_candles = [c for c in all_candles if datetime_to_ms(c.timestamp) < end_ms]
        validation_error = validate_candles(all_candles)
        if validation_error:
            return data_error_result(self.name, validation_error)

        logger.info(
            f"[{self.name}] batch_ohlcv {binance_symbol} {interval}: "
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

    # ---- 合约扩展方法（fapi 公共接口）----

    async def get_funding_rate(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取永续资金费率：GET https://fapi.binance.com/fapi/v1/premiumIndex。"""
        try:
            binance_symbol = to_binance(symbol)
            normalized = normalize_symbol(symbol)
        except ValueError as e:
            return data_error_result(self.name, str(e))

        result = await self._request(
            "GET", f"{FUTURES_BASE_URL}/fapi/v1/premiumIndex",
            params={"symbol": binance_symbol},
        )
        if not result.success:
            return result

        raw = result.data
        if not isinstance(raw, dict):
            return parse_error_result(self.name, f"期望 dict，得到 {type(raw).__name__}", raw=raw)

        funding_rate = safe_float(raw.get("lastFundingRate"))
        if funding_rate is None:
            return data_error_result(self.name, "lastFundingRate 缺失", raw=raw)

        data = FundingData(
            symbol=normalized,
            funding_rate=funding_rate,
            mark_price=safe_float(raw.get("markPrice")),
            index_price=safe_float(raw.get("indexPrice")),
            next_funding_time=ms_to_datetime(raw.get("nextFundingTime")),
            timestamp=ms_to_datetime(raw.get("time")) or utcnow(),
            source=self.name,
        )
        return success_result(
            result, data, observation_time=data.timestamp
        )

    async def get_open_interest(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取未平仓合约量：GET https://fapi.binance.com/fapi/v1/openInterest。"""
        try:
            binance_symbol = to_binance(symbol)
            normalized = normalize_symbol(symbol)
        except ValueError as e:
            return data_error_result(self.name, str(e))

        result = await self._request(
            "GET", f"{FUTURES_BASE_URL}/fapi/v1/openInterest",
            params={"symbol": binance_symbol},
        )
        if not result.success:
            return result

        raw = result.data
        if not isinstance(raw, dict):
            return parse_error_result(self.name, f"期望 dict，得到 {type(raw).__name__}", raw=raw)

        oi = safe_float(raw.get("openInterest"))
        if oi is None:
            return data_error_result(self.name, "openInterest 缺失", raw=raw)

        timestamp = ms_to_datetime(raw.get("time")) or utcnow()
        mark_price: float | None = None
        oi_value: float | None = None

        # 附加标记价格以估算名义价值（失败不阻断主流程）
        premium = await self._request(
            "GET", f"{FUTURES_BASE_URL}/fapi/v1/premiumIndex",
            params={"symbol": binance_symbol},
        )
        if premium.success and isinstance(premium.data, dict):
            mark_price = safe_float(premium.data.get("markPrice"))
            if mark_price:
                oi_value = oi * mark_price

        data = OpenInterestData(
            symbol=normalized,
            open_interest=oi,
            open_interest_value=oi_value,
            timestamp=timestamp,
            source=self.name,
        )
        return success_result(
            result, data, observation_time=timestamp,
            quality_status=QualityStatus.VERIFIED if oi_value else QualityStatus.ESTIMATED,
            metadata={"mark_price": mark_price},
        )

    async def get_liquidations(self, symbol: str = "BTC/USDT", limit: int = 100) -> FetchResult:
        """获取强平订单：GET https://fapi.binance.com/fapi/v1/allForceOrders。

        注意：Binance 自 2021 起仅推送最近 7 天的少量强平快照，
        该接口经常返回空列表，属正常情况（success=True, data=[]）。
        """
        try:
            binance_symbol = to_binance(symbol)
            normalized = normalize_symbol(symbol)
        except ValueError as e:
            return data_error_result(self.name, str(e))

        result = await self._request(
            "GET", f"{FUTURES_BASE_URL}/fapi/v1/allForceOrders",
            params={"symbol": binance_symbol, "limit": max(1, min(limit, 1000))},
        )
        if not result.success:
            return result

        raw = result.data
        if not isinstance(raw, list):
            return parse_error_result(self.name, f"期望 list，得到 {type(raw).__name__}", raw=raw)

        orders: list[LiquidationOrder] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            price = safe_float(item.get("price"))
            qty = safe_float(item.get("origQty"))
            if price is None or qty is None:
                continue
            orders.append(LiquidationOrder(
                symbol=normalized,
                side="buy" if str(item.get("side", "")).upper() == "BUY" else "sell",
                price=price,
                quantity=qty,
                timestamp=ms_to_datetime(item.get("time")),
            ))

        return success_result(
            result, orders,
            observation_time=utcnow(),
            quality_status=QualityStatus.VERIFIED if orders else QualityStatus.ESTIMATED,
            metadata={"count": len(orders)},
        )

    # ---- 内部解析辅助 ----

    def _parse_klines(self, raw: object) -> tuple[list[Candle], str | None]:
        """解析 /api/v3/klines 响应为标准化 Candle 列表。

        Binance kline 数组字段顺序：
        [0]open_time [1]open [2]high [3]low [4]close [5]volume [6]close_time
        [7]quote_volume [8]trades [9]taker_buy_base [10]taker_buy_quote [11]ignore
        """
        if not isinstance(raw, list):
            return [], f"期望 list，得到 {type(raw).__name__}"

        now = utcnow()
        candles: list[Candle] = []
        for row in raw:
            if not isinstance(row, list) or len(row) < 11:
                length = len(row) if isinstance(row, list) else "N/A"
                return [], f"kline 行格式非法（长度 {length}）"

            ts = ms_to_datetime(row[0])
            o, h, low, c = (safe_float(row[i]) for i in (1, 2, 3, 4))
            vol = safe_float(row[5])
            if ts is None or None in (o, h, low, c) or vol is None:
                return [], "kline 数值字段非法"

            close_time = ms_to_datetime(row[6])
            is_closed = close_time is not None and close_time < now

            candles.append(Candle(
                timestamp=ts, open=o, high=h, low=low, close=c, volume=vol,
                quote_volume=safe_float(row[7]),
                trades=safe_int(row[8]),
                taker_buy_volume=safe_float(row[9]),
                vwap=None,
                is_closed=is_closed,
            ))
        return candles, None

    @staticmethod
    def _parse_24hr(raw: dict, normalized_symbol: str) -> dict | None:
        """解析 /api/v3/ticker/24hr 响应为标准化 dict。"""
        price = safe_float(raw.get("lastPrice"))
        if price is None:
            return None
        return {
            "symbol": normalized_symbol,
            "last_price": price,
            "price_change": safe_float(raw.get("priceChange")),
            "price_change_percent": safe_float(raw.get("priceChangePercent")),
            "weighted_avg_price": safe_float(raw.get("weightedAvgPrice")),
            "high": safe_float(raw.get("highPrice")),
            "low": safe_float(raw.get("lowPrice")),
            "open": safe_float(raw.get("openPrice")),
            "volume_base": safe_float(raw.get("volume")) or 0.0,
            "volume_quote": safe_float(raw.get("quoteVolume")) or 0.0,
            "trades": safe_int(raw.get("count")),
            "close_time": ms_to_datetime(raw.get("closeTime")),
        }

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


__all__ = ["BinanceProvider"]
