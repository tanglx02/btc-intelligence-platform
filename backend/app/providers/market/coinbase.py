"""CoinbaseProvider — Coinbase Exchange 现货市场行情 Provider（优先级 3）。

API 文档：https://docs.cloud.coinbase.com/exchange/reference/

数据覆盖：价格 / K线 / 24h统计 / 订单簿 / 成交 / 价差 / CVD / 市值 / ATH
不支持：资金费率 / 未平仓量 / 强平（Coinbase Exchange 无衍生品公共接口）

注意事项：
- base_url 应配置为 https://api.exchange.coinbase.com（Exchange API），
  若配置为其他域名（如 api.coinbase.com），本 Provider 自动覆盖为 Exchange API。
- Symbol 格式：BTC-USD（无 BTC-USDT 现货对，USDT 计价自动回退 USD）。
- candles 接口按时间倒序返回、单次最多 300 根、granularity 为秒。
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
    as_naive_utc,
    data_error_result,
    empty_result,
    parse_error_result,
    parse_iso_datetime,
    safe_float,
    sec_to_datetime,
    success_result,
    utcnow,
    validate_candles,
    window_start,
)
from app.providers.market.symbol_mapper import normalize_symbol, to_coinbase
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

# Exchange API 标准域名
EXCHANGE_BASE_URL = "https://api.exchange.coinbase.com"

# 内部标准间隔 -> Coinbase granularity（秒）
COINBASE_GRANULARITY: dict[str, int] = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "6h": 21600, "1d": 86400,
}

# candles 接口单次最大条数
_MAX_CANDLES = 300
# 批量拉取最大分页数
_MAX_PAGES = 400
# Coinbase Exchange BTC-USD 上线时间（ATH 扫描起点）
_BTCUSD_LISTING = datetime(2015, 1, 26)


class CoinbaseProvider(BaseMarketProvider):
    """Coinbase Exchange 市场行情 Provider。"""

    def __init__(self, config: ProviderConfig):
        # 强制使用 Exchange API 域名（行情接口不在 api.coinbase.com 上）
        if "exchange.coinbase.com" not in config.base_url:
            logger.warning(
                f"[{config.name}] base_url '{config.base_url}' 不是 Exchange API，"
                f"已自动覆盖为 {EXCHANGE_BASE_URL}"
            )
            config.base_url = EXCHANGE_BASE_URL
        super().__init__(config)

    # ---- BaseProvider 抽象方法 ----

    async def health_check(self) -> bool:
        """轻量级连通性探测：GET /products（返回交易对列表）。"""
        result = await self._request("GET", "/products")
        healthy = result.success and isinstance(result.data, list) and len(result.data) > 0
        if not healthy:
            logger.warning(f"[{self.name}] Health check failed: {result.error}")
        return healthy

    def get_metadata(self) -> ProviderMetadata:
        """返回 Provider 元信息。"""
        return ProviderMetadata(
            name=self.name,
            category=self.category,
            description=self._config.description or "Coinbase 现货行情（合规标杆）",
            supported_symbols=self._config.supported_symbols or ["BTC/USD"],
            supported_intervals=self._config.supported_intervals
            or ["1m", "5m", "15m", "1h", "6h", "1d"],
            data_coverage_start=_BTCUSD_LISTING,
            documentation_url="https://docs.cloud.coinbase.com/exchange/reference/",
        )

    def get_supported_data_types(self) -> list[str]:
        """返回支持的数据类型列表。"""
        return [
            "price", "ohlcv", "stats_24h", "volume", "market_cap", "ath",
            "order_book", "trades", "spread", "cvd",
        ]

    # ---- BaseMarketProvider 抽象方法 ----

    async def get_current_price(self, symbol: str = "BTC/USD") -> FetchResult:
        """获取当前价格：GET /products/{product_id}/ticker。"""
        product_id, error = self._resolve_product_id(symbol)
        if error:
            return data_error_result(self.name, error)

        result = await self._request("GET", f"/products/{product_id}/ticker")
        if not result.success:
            return result

        raw = result.data
        if not isinstance(raw, dict):
            return parse_error_result(self.name, f"期望 dict，得到 {type(raw).__name__}", raw=raw)

        price = safe_float(raw.get("price"))
        if price is None or price <= 0:
            return data_error_result(self.name, f"非法价格: {raw.get('price')!r}", raw=raw)

        now = parse_iso_datetime(raw.get("time")) or utcnow()
        data = PriceData(
            symbol=normalize_symbol(symbol), price=price,
            bid=safe_float(raw.get("bid")), ask=safe_float(raw.get("ask")),
            timestamp=now, source=self.name,
        )
        logger.debug(f"[{self.name}] get_current_price {product_id} = {price}")
        return success_result(result, data, observation_time=now)

    async def get_ohlcv(
        self,
        symbol: str = "BTC/USD",
        interval: str = "1h",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> FetchResult:
        """获取 K 线：GET /products/{product_id}/candles。

        granularity 只支持 60/300/900/3600/21600/86400；
        单次最多返回 300 根（超出部分需使用 get_batch_ohlcv 分页）。
        """
        product_id, error = self._resolve_product_id(symbol)
        if error:
            return data_error_result(self.name, error)

        granularity = COINBASE_GRANULARITY.get(interval.lower())
        if not granularity:
            return data_error_result(
                self.name,
                f"不支持的K线间隔: {interval}（可用: {sorted(COINBASE_GRANULARITY)}）",
            )

        # 未指定 start 时按 limit 推算，保证请求窗口内数据完整
        if end is None:
            end = utcnow()
        if start is None:
            start = end - timedelta(seconds=granularity * min(limit, _MAX_CANDLES))

        result = await self._request(
            "GET", f"/products/{product_id}/candles",
            params={
                "granularity": str(granularity),
                "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        if not result.success:
            return result

        candles = self._parse_candles(result.data)
        if candles is None:
            return parse_error_result(self.name, "candles 格式非法", raw=result.data)
        if not candles:
            return empty_result(self.name, "candles 返回为空", raw=result.data)

        if len(candles) > limit:
            candles = candles[-limit:]

        validation_error = validate_candles(candles)
        if validation_error:
            return data_error_result(self.name, validation_error, raw=result.data)

        logger.debug(f"[{self.name}] get_ohlcv {product_id} {interval}: {len(candles)} candles")
        return success_result(
            result, candles,
            observation_time=candles[-1].timestamp,
            metadata={"count": len(candles), "interval": interval},
        )

    async def get_24h_stats(self, symbol: str = "BTC/USD") -> FetchResult:
        """获取 24h 统计：GET /products/{product_id}/stats。"""
        product_id, error = self._resolve_product_id(symbol)
        if error:
            return data_error_result(self.name, error)

        result = await self._request("GET", f"/products/{product_id}/stats")
        if not result.success:
            return result

        raw = result.data
        if not isinstance(raw, dict):
            return parse_error_result(self.name, f"期望 dict，得到 {type(raw).__name__}", raw=raw)

        open_24h = safe_float(raw.get("open"))
        last = safe_float(raw.get("last"))
        high = safe_float(raw.get("high"))
        low = safe_float(raw.get("low"))
        volume = safe_float(raw.get("volume"))
        if open_24h is None or high is None or low is None or volume is None:
            return data_error_result(self.name, "stats 关键字段缺失", raw=raw)

        stats = {
            "symbol": normalize_symbol(symbol),
            "last_price": last,
            "price_change": (last - open_24h) if last is not None else None,
            "price_change_percent": (
                (last - open_24h) / open_24h * 100.0
                if last is not None and open_24h
                else None
            ),
            "weighted_avg_price": None,
            "high": high,
            "low": low,
            "open": open_24h,
            "volume_base": volume,
            # stats 的 volume 为基础资产数量，USD 成交额用均价近似
            "volume_quote": volume * ((high + low) / 2.0),
            "trades": None,
            "close_time": parse_iso_datetime(raw.get("end_time")) or utcnow(),
        }
        return success_result(
            result, stats,
            observation_time=parse_iso_datetime(raw.get("end_time")) or utcnow(),
        )

    async def get_volume(self, symbol: str = "BTC/USD", interval: str = "24h") -> FetchResult:
        """获取成交量（基于 /stats 24h 数据）。"""
        product_id, error = self._resolve_product_id(symbol)
        if error:
            return data_error_result(self.name, error)

        result = await self._request("GET", f"/products/{product_id}/stats")
        if not result.success:
            return result

        raw = result.data
        if not isinstance(raw, dict):
            return parse_error_result(self.name, f"期望 dict，得到 {type(raw).__name__}", raw=raw)

        base_volume = safe_float(raw.get("volume"))
        if base_volume is None:
            return data_error_result(self.name, "volume 缺失", raw=raw)

        high = safe_float(raw.get("high"))
        low = safe_float(raw.get("low"))
        data = VolumeData(
            symbol=normalize_symbol(symbol),
            base_volume=base_volume,
            quote_volume=base_volume * ((high + low) / 2.0) if high and low else None,
            interval="24h",
            timestamp=parse_iso_datetime(raw.get("end_time")) or utcnow(),
            source=self.name,
        )
        return success_result(
            result, data,
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
        """获取历史最高价（扫描 BTC-USD 上线以来的日K线）。

        注意：Coinbase candles 接口对久远历史数据的保留有限，
        结果标记为 ESTIMATED。
        """
        asset = symbol.strip().upper().split("/")[0]
        if asset == "XBT":
            asset = "BTC"

        batch = await self.get_batch_ohlcv(
            f"{asset}/USD", "1d", _BTCUSD_LISTING, utcnow() + timedelta(days=1)
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
                "scan_start": _BTCUSD_LISTING.isoformat(),
                "scanned_candles": len(candles),
                "note": "Coinbase 历史数据保留有限，ATH 可能不完整",
            },
        )

    async def get_order_book(self, symbol: str = "BTC/USD", depth: int = 20) -> FetchResult:
        """获取订单簿：GET /products/{product_id}/book?level=2。"""
        product_id, error = self._resolve_product_id(symbol)
        if error:
            return data_error_result(self.name, error)

        result = await self._request(
            "GET", f"/products/{product_id}/book", params={"level": "2"}
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

        now = parse_iso_datetime(raw.get("time")) or utcnow()
        data = OrderBookData(
            symbol=normalize_symbol(symbol), bids=bids, asks=asks,
            timestamp=now, source=self.name,
            last_update_id=str(raw.get("sequence", "")),
        )
        return success_result(result, data, observation_time=now)

    async def get_recent_trades(self, symbol: str = "BTC/USD", limit: int = 50) -> FetchResult:
        """获取最近成交：GET /products/{product_id}/trades。"""
        product_id, error = self._resolve_product_id(symbol)
        if error:
            return data_error_result(self.name, error)

        result = await self._request("GET", f"/products/{product_id}/trades")
        if not result.success:
            return result

        raw = result.data
        if not isinstance(raw, list):
            return parse_error_result(self.name, f"期望 list，得到 {type(raw).__name__}", raw=raw)

        trades: list[Trade] = []
        for item in raw[: max(1, min(limit, 1000))]:
            if not isinstance(item, dict):
                continue
            price = safe_float(item.get("price"))
            qty = safe_float(item.get("size"))
            if price is None or qty is None:
                continue
            side = str(item.get("side", "")).lower()  # buy/sell（maker 视角）
            trades.append(Trade(
                trade_id=str(item.get("trade_id", "")),
                price=price,
                quantity=qty,
                timestamp=parse_iso_datetime(item.get("time")),
                # side 为 maker 方向，taker 方向取反
                side=("sell" if side == "buy" else "buy") if side in ("buy", "sell") else None,
                is_buyer_maker=(side == "buy") if side in ("buy", "sell") else None,
                quote_quantity=price * qty,
            ))

        if not trades:
            return empty_result(self.name, "trades 返回为空", raw=raw)
        return success_result(
            result, trades,
            observation_time=trades[0].timestamp,
            metadata={"count": len(trades)},
        )

    async def get_spread(self, symbol: str = "BTC/USD") -> FetchResult:
        """获取买卖价差（基于 ticker 的 bid/ask）。"""
        product_id, error = self._resolve_product_id(symbol)
        if error:
            return data_error_result(self.name, error)

        result = await self._request("GET", f"/products/{product_id}/ticker")
        if not result.success:
            return result

        raw = result.data
        if not isinstance(raw, dict):
            return parse_error_result(self.name, f"期望 dict，得到 {type(raw).__name__}", raw=raw)

        bid = safe_float(raw.get("bid"))
        ask = safe_float(raw.get("ask"))
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return data_error_result(self.name, f"非法盘口: bid={bid}, ask={ask}", raw=raw)

        mid = (bid + ask) / 2.0
        now = parse_iso_datetime(raw.get("time")) or utcnow()
        data = SpreadData(
            symbol=normalize_symbol(symbol), bid=bid, ask=ask,
            spread=ask - bid,
            spread_percent=(ask - bid) / mid * 100.0 if mid > 0 else 0.0,
            mid_price=mid, timestamp=now, source=self.name,
        )
        return success_result(result, data, observation_time=now)

    async def get_cvd(self, symbol: str = "BTC/USD", interval: str = "1h") -> FetchResult:
        """获取现货 CVD（聚合最近成交，Coinbase fills 上限 100 条，为粗略近似）。"""
        trades_result = await self.get_recent_trades(symbol, limit=100)
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
            metadata={"note": "基于最近 100 笔成交聚合，仅为粗略近似"},
        )

    async def get_batch_ohlcv(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> FetchResult:
        """批量获取历史 K 线（按 300 根/页向历史方向分页）。"""
        product_id, error = self._resolve_product_id(symbol)
        if error:
            return data_error_result(self.name, error)

        granularity = COINBASE_GRANULARITY.get(interval.lower())
        if not granularity:
            return data_error_result(self.name, f"不支持的K线间隔: {interval}")

        all_candles: list[Candle] = []
        last_result: FetchResult | None = None
        # 游标：当前分页窗口的右边界（end 侧）
        cursor_end = end
        pages = 0

        while pages < _MAX_PAGES:
            cursor_start = cursor_end - timedelta(seconds=granularity * _MAX_CANDLES)
            if cursor_start < start:
                cursor_start = start

            result = await self._request(
                "GET", f"/products/{product_id}/candles",
                params={
                    "granularity": str(granularity),
                    "start": cursor_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end": cursor_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
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
            candles = self._parse_candles(result.data)
            if candles is None:
                return parse_error_result(self.name, "candles 格式非法", raw=result.data)

            if candles:
                all_candles = candles + all_candles  # 前插（分页向历史方向推进）
                pages += 1

            if cursor_start <= start:
                break
            # 下一页右边界 = 当前窗口左边界（-1 秒避免重叠）
            cursor_end = cursor_start - timedelta(seconds=1)

        if not all_candles:
            return empty_result(self.name, f"批量K线为空: {product_id} {interval}")

        # 去重（按 timestamp）+ 范围过滤 + 升序排序（边界统一为 naive）
        deduped: dict[datetime, Candle] = {}
        for c in all_candles:
            if as_naive_utc(start) <= c.timestamp < as_naive_utc(end):
                deduped[c.timestamp] = c
        final = [deduped[k] for k in sorted(deduped)]

        if not final:
            return empty_result(self.name, f"时间范围内无K线: {product_id} {interval}")

        validation_error = validate_candles(final)
        if validation_error:
            return data_error_result(self.name, validation_error)

        logger.info(
            f"[{self.name}] batch_ohlcv {product_id} {interval}: "
            f"{len(final)} candles in {pages} pages"
        )
        base = last_result or FetchResult(
            success=True, provider_name=self.name, fetch_time=utcnow()
        )
        return success_result(
            base, final,
            observation_time=final[-1].timestamp,
            metadata={"count": len(final), "pages": pages, "interval": interval},
        )

    # ---- 内部辅助 ----

    @staticmethod
    def _resolve_product_id(symbol: str) -> tuple[str | None, str | None]:
        """解析内部 symbol 为 Coinbase product_id（USDT 计价回退 USD）。"""
        try:
            return to_coinbase(symbol), None
        except ValueError as e:
            return None, str(e)

    @staticmethod
    def _parse_candles(raw: object) -> list[Candle] | None:
        """解析 /candles 响应（倒序输入 -> 升序输出）。

        行格式：[time(秒), low, high, open, close, volume]
        """
        if not isinstance(raw, list):
            return None

        candles: list[Candle] = []
        for row in raw:
            if not isinstance(row, list) or len(row) < 6:
                return None
            ts = sec_to_datetime(row[0])
            low, high, o, c = (safe_float(row[i]) for i in (1, 2, 3, 4))
            vol = safe_float(row[5])
            if ts is None or None in (o, high, low, c) or vol is None:
                return None
            candles.append(Candle(
                timestamp=ts, open=o, high=high, low=low, close=c,
                volume=vol, quote_volume=vol * c, trades=None, is_closed=True,
            ))
        candles.reverse()  # Coinbase 返回倒序
        return candles

    @staticmethod
    def _parse_book_levels(levels: object, depth: int) -> list[list[float]] | None:
        """解析 level=2 订单簿档位 [[price, size, num-orders], ...]。"""
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


__all__ = ["CoinbaseProvider"]
