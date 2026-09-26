"""OKXProvider — OKX 现货市场行情 Provider（优先级 2）。

API 文档：https://www.okx.com/docs-v5/en/

数据覆盖：
- 价格 / K线 / 24h统计 / 订单簿 / 成交 / 价差 / CVD / 市值 / ATH
- 永续合约公共接口：资金费率 / 未平仓量（instId 自动映射为 *-SWAP）

响应格式约定：{"code": "0", "msg": "", "data": [...]}，code != "0" 视为 API 错误。
Symbol 格式：BTC-USDT（内部 "BTC/USDT" 自动转换；永续为 BTC-USDT-SWAP）
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
from app.providers.market.symbol_mapper import (
    normalize_symbol,
    split_symbol,
    to_okx,
    to_okx_quote_fallback,
)
from app.providers.market.types import (
    ATHData,
    Candle,
    CVDData,
    FundingData,
    MarketCapData,
    OpenInterestData,
    OrderBookData,
    PriceData,
    SpreadData,
    Trade,
    VolumeData,
)

# 内部标准间隔 -> OKX bar 参数
OKX_INTERVALS: dict[str, str] = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H", "12h": "12H",
    "1d": "1D", "1w": "1W",
}

# OKX 历史数据最早可追溯时间（BTC-USDT 于 2018 年上线 OKX）
_OKX_COVERAGE_START = datetime(2018, 3, 21)

# 单次 candles 最大条数 / 批量拉取最大分页数
_MAX_LIMIT = 300
_MAX_PAGES = 300


class OKXProvider(BaseMarketProvider):
    """OKX 市场行情 Provider。"""

    # 注册名（与 providers.yaml 配置 key 对齐；snake_case 推导会产生 o_k_x）
    provider_key = "okx"

    def __init__(self, config: ProviderConfig):
        super().__init__(config)

    # ---- BaseProvider 抽象方法 ----

    async def health_check(self) -> bool:
        """轻量级连通性探测：GET /api/v5/public/time。"""
        result = await self._request("GET", "/api/v5/public/time")
        if not result.success:
            logger.warning(f"[{self.name}] Health check failed: {result.error}")
            return False
        return self._check_api_code(result) is None

    def get_metadata(self) -> ProviderMetadata:
        """返回 Provider 元信息。"""
        return ProviderMetadata(
            name=self.name,
            category=self.category,
            description=self._config.description or "OKX 现货行情",
            supported_symbols=self._config.supported_symbols or ["BTC/USDT"],
            supported_intervals=self._config.supported_intervals
            or ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"],
            data_coverage_start=_OKX_COVERAGE_START,
            documentation_url="https://www.okx.com/docs-v5/en/",
        )

    def get_supported_data_types(self) -> list[str]:
        """返回支持的数据类型列表。"""
        return [
            "price", "ohlcv", "stats_24h", "volume", "market_cap", "ath",
            "order_book", "trades", "spread", "cvd",
            "funding_rate", "open_interest",
        ]

    # ---- BaseMarketProvider 抽象方法 ----

    async def get_current_price(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取当前价格：GET /api/v5/market/ticker?instId=BTC-USDT。"""
        ticker = await self._fetch_ticker(symbol)
        if not ticker.success:
            return ticker

        raw = ticker.data["raw"]
        normalized = ticker.data["normalized"]

        price = safe_float(raw.get("last"))
        if price is None or price <= 0:
            return data_error_result(self.name, f"非法价格: {raw.get('last')!r}", raw=raw)

        now = ms_to_datetime(raw.get("ts")) or utcnow()
        data = PriceData(
            symbol=normalized, price=price,
            bid=safe_float(raw.get("bidPx")), ask=safe_float(raw.get("askPx")),
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
        """获取 K 线：GET /api/v5/market/candles（最近 1440 条）。

        注意：OKX candles 接口按 end 时间倒序返回，此处已标准化为升序。
        """
        inst_id, error = self._resolve_inst_id(symbol)
        if error:
            return data_error_result(self.name, error)

        bar = OKX_INTERVALS.get(interval.lower())
        if not bar:
            return data_error_result(
                self.name, f"不支持的K线间隔: {interval}（可用: {sorted(OKX_INTERVALS)}）"
            )

        params: dict = {"instId": inst_id, "bar": bar, "limit": min(max(limit, 1), 300)}
        if end:
            # OKX: after = 返回早于该时间的数据（往前翻）
            params["after"] = str(datetime_to_ms(end))
        if start:
            # OKX: before = 返回晚于该时间的数据
            params["before"] = str(datetime_to_ms(align_to_interval(start, interval)))

        result = await self._request("GET", "/api/v5/market/candles", params=params)
        if not result.success:
            return result

        api_error = self._check_api_code(result)
        if api_error:
            return api_error

        candles = self._parse_candles(result.data.get("data", []))
        if not candles:
            return empty_result(self.name, "candles 返回为空", raw=result.data)

        validation_error = validate_candles(candles)
        if validation_error:
            return data_error_result(self.name, validation_error, raw=result.data)

        # 应用 start/end 过滤并截断
        candles = self._filter_range(candles, start, end, limit)
        if not candles:
            return empty_result(self.name, "时间范围内无K线数据", raw=result.data)

        logger.debug(f"[{self.name}] get_ohlcv {inst_id} {interval}: {len(candles)} candles")
        return success_result(
            result, candles,
            observation_time=candles[-1].timestamp,
            metadata={"count": len(candles), "interval": interval},
        )

    async def get_24h_stats(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取 24h 统计：GET /api/v5/market/ticker。"""
        ticker = await self._fetch_ticker(symbol)
        if not ticker.success:
            return ticker

        raw = ticker.data["raw"]
        normalized = ticker.data["normalized"]

        last = safe_float(raw.get("last"))
        open_24h = safe_float(raw.get("open24h"))
        high = safe_float(raw.get("high24h"))
        low = safe_float(raw.get("low24h"))
        if last is None or open_24h is None:
            return data_error_result(self.name, "ticker 关键字段缺失", raw=raw)

        stats = {
            "symbol": normalized,
            "last_price": last,
            "price_change": last - open_24h,
            "price_change_percent": (last - open_24h) / open_24h * 100.0 if open_24h else None,
            "weighted_avg_price": None,
            "high": high,
            "low": low,
            "open": open_24h,
            "volume_base": safe_float(raw.get("vol24h")) or 0.0,
            "volume_quote": safe_float(raw.get("volCcy24h")) or 0.0,
            "trades": None,
            "close_time": ms_to_datetime(raw.get("ts")),
        }
        return success_result(
            ticker, stats,
            observation_time=ms_to_datetime(raw.get("ts")) or utcnow(),
        )

    async def get_volume(self, symbol: str = "BTC/USDT", interval: str = "24h") -> FetchResult:
        """获取成交量（基于 24h ticker）。"""
        ticker = await self._fetch_ticker(symbol)
        if not ticker.success:
            return ticker

        raw = ticker.data["raw"]
        base_volume = safe_float(raw.get("vol24h"))
        if base_volume is None:
            return data_error_result(self.name, "vol24h 缺失", raw=raw)

        data = VolumeData(
            symbol=ticker.data["normalized"],
            base_volume=base_volume,
            quote_volume=safe_float(raw.get("volCcy24h")),
            interval="24h",
            timestamp=ms_to_datetime(raw.get("ts")) or utcnow(),
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
        """获取历史最高价（扫描 OKX 数据覆盖起点以来的日K线）。"""
        asset = symbol.strip().upper().split("/")[0]
        if asset == "XBT":
            asset = "BTC"

        batch = await self.get_batch_ohlcv(
            f"{asset}/USDT", "1d", _OKX_COVERAGE_START, utcnow() + timedelta(days=1)
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
                "scan_start": _OKX_COVERAGE_START.isoformat(),
                "scanned_candles": len(candles),
            },
        )

    async def get_order_book(self, symbol: str = "BTC/USDT", depth: int = 20) -> FetchResult:
        """获取订单簿：GET /api/v5/market/books。"""
        inst_id, error = self._resolve_inst_id(symbol)
        if error:
            return data_error_result(self.name, error)

        result = await self._request(
            "GET", "/api/v5/market/books",
            params={"instId": inst_id, "sz": str(min(max(depth, 1), 400))},
        )
        if not result.success:
            return result

        api_error = self._check_api_code(result)
        if api_error:
            return api_error

        payload = (result.data.get("data") or [{}])[0]
        bids = self._parse_book_levels(payload.get("bids"), depth)
        asks = self._parse_book_levels(payload.get("asks"), depth)
        if bids is None or asks is None:
            return parse_error_result(self.name, "bids/asks 格式非法", raw=result.data)
        if not bids or not asks:
            return empty_result(self.name, "订单簿为空", raw=result.data)

        now = ms_to_datetime(payload.get("ts")) or utcnow()
        data = OrderBookData(
            symbol=normalize_symbol(symbol), bids=bids, asks=asks,
            timestamp=now, source=self.name,
            last_update_id=str(payload.get("checksum", "")),
        )
        return success_result(result, data, observation_time=now)

    async def get_recent_trades(self, symbol: str = "BTC/USDT", limit: int = 50) -> FetchResult:
        """获取最近成交：GET /api/v5/market/trades。"""
        inst_id, error = self._resolve_inst_id(symbol)
        if error:
            return data_error_result(self.name, error)

        result = await self._request(
            "GET", "/api/v5/market/trades",
            params={"instId": inst_id, "limit": str(min(max(limit, 1), 500))},
        )
        if not result.success:
            return result

        api_error = self._check_api_code(result)
        if api_error:
            return api_error

        trades: list[Trade] = []
        for item in result.data.get("data", []):
            if not isinstance(item, dict):
                continue
            price = safe_float(item.get("px"))
            qty = safe_float(item.get("sz"))
            if price is None or qty is None:
                continue
            side = str(item.get("side", "")).lower()
            trades.append(Trade(
                trade_id=str(item.get("tradeId", "")),
                price=price,
                quantity=qty,
                timestamp=ms_to_datetime(item.get("ts")),
                side=side or None,
                is_buyer_maker=(side == "sell") if side else None,
            ))

        if not trades:
            return empty_result(self.name, "trades 返回为空", raw=result.data)
        return success_result(
            result, trades,
            observation_time=trades[0].timestamp,
            metadata={"count": len(trades)},
        )

    async def get_spread(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取买卖价差（基于 ticker 的 bidPx/askPx）。"""
        ticker = await self._fetch_ticker(symbol)
        if not ticker.success:
            return ticker

        raw = ticker.data["raw"]
        bid = safe_float(raw.get("bidPx"))
        ask = safe_float(raw.get("askPx"))
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return data_error_result(self.name, f"非法盘口: bid={bid}, ask={ask}", raw=raw)

        mid = (bid + ask) / 2.0
        now = ms_to_datetime(raw.get("ts")) or utcnow()
        data = SpreadData(
            symbol=ticker.data["normalized"], bid=bid, ask=ask,
            spread=ask - bid,
            spread_percent=(ask - bid) / mid * 100.0 if mid > 0 else 0.0,
            mid_price=mid, timestamp=now, source=self.name,
        )
        return success_result(ticker, data, observation_time=now)

    async def get_cvd(self, symbol: str = "BTC/USDT", interval: str = "1h") -> FetchResult:
        """获取现货 CVD（聚合最近 500 笔成交，近似值）。"""
        trades_result = await self.get_recent_trades(symbol, limit=500)
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
            metadata={"note": "基于最近 500 笔成交聚合，长窗口为近似值"},
        )

    async def get_batch_ohlcv(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> FetchResult:
        """批量获取历史 K 线（使用 history-candles 接口向后分页）。"""
        inst_id, error = self._resolve_inst_id(symbol)
        if error:
            return data_error_result(self.name, error)

        bar = OKX_INTERVALS.get(interval.lower())
        if not bar:
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
                "GET", "/api/v5/market/history-candles",
                params={
                    "instId": inst_id,
                    "bar": bar,
                    # before = 返回记录时间早于该时间戳 -> 向后翻页
                    "before": str(cursor_ms),
                    "limit": str(_MAX_LIMIT),
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

            api_error = self._check_api_code(result)
            if api_error:
                if all_candles:
                    break
                return api_error

            last_result = result
            # OKX 返回按时间倒序，翻转为升序
            candles = list(reversed(self._parse_candles(result.data.get("data", []))))
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
            return empty_result(self.name, f"批量K线为空: {inst_id} {interval}")

        all_candles = [c for c in all_candles if datetime_to_ms(c.timestamp) < end_ms]
        validation_error = validate_candles(all_candles)
        if validation_error:
            return data_error_result(self.name, validation_error)

        logger.info(
            f"[{self.name}] batch_ohlcv {inst_id} {interval}: "
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

    # ---- 合约公共接口扩展方法 ----

    async def get_funding_rate(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取永续资金费率：GET /api/v5/public/funding-rate（instId 映射为 *-SWAP）。"""
        try:
            inst_id = to_okx(symbol, swap=True)
            normalized = normalize_symbol(symbol)
        except ValueError as e:
            return data_error_result(self.name, str(e))

        result = await self._request(
            "GET", "/api/v5/public/funding-rate", params={"instId": inst_id}
        )
        if not result.success:
            return result

        api_error = self._check_api_code(result)
        if api_error:
            return api_error

        payload_list = result.data.get("data") or []
        if not payload_list:
            return empty_result(self.name, f"funding-rate 返回为空: {inst_id}", raw=result.data)

        raw = payload_list[0]
        funding_rate = safe_float(raw.get("fundingRate"))
        if funding_rate is None:
            return data_error_result(self.name, "fundingRate 缺失", raw=raw)

        data = FundingData(
            symbol=normalized,
            funding_rate=funding_rate,
            mark_price=None,
            index_price=None,
            next_funding_time=ms_to_datetime(raw.get("nextFundingTime")),
            timestamp=ms_to_datetime(raw.get("ts")) or utcnow(),
            source=self.name,
        )
        return success_result(result, data, observation_time=data.timestamp)

    async def get_open_interest(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取未平仓合约量：GET /api/v5/public/open-interest（instId 映射为 *-SWAP）。"""
        try:
            inst_id = to_okx(symbol, swap=True)
            normalized = normalize_symbol(symbol)
        except ValueError as e:
            return data_error_result(self.name, str(e))

        result = await self._request(
            "GET", "/api/v5/public/open-interest",
            params={"instType": "SWAP", "instId": inst_id},
        )
        if not result.success:
            return result

        api_error = self._check_api_code(result)
        if api_error:
            return api_error

        payload_list = result.data.get("data") or []
        if not payload_list:
            return empty_result(self.name, f"open-interest 返回为空: {inst_id}", raw=result.data)

        raw = payload_list[0]
        # OKX 的 oi 字段单位为币（BTC），oiCcy 为合约张数对应币数
        oi = safe_float(raw.get("oiCcy")) or safe_float(raw.get("oi"))
        if oi is None:
            return data_error_result(self.name, "oi/oiCcy 缺失", raw=raw)

        timestamp = ms_to_datetime(raw.get("ts")) or utcnow()
        data = OpenInterestData(
            symbol=normalized,
            open_interest=oi,
            open_interest_value=None,
            timestamp=timestamp,
            source=self.name,
        )
        return success_result(result, data, observation_time=timestamp)

    # ---- 内部辅助 ----

    def _resolve_inst_id(self, symbol: str) -> tuple[str | None, str | None]:
        """解析内部 symbol 为 OKX instId（USD 计价回退 USDT）。"""
        try:
            base, quote = split_symbol(symbol)
        except ValueError as e:
            return None, str(e)
        if quote == "USD":
            # OKX 现货无 USD 计价对，回退 USDT
            return to_okx_quote_fallback(symbol), None
        return to_okx(symbol), None

    async def _fetch_ticker(self, symbol: str) -> FetchResult:
        """获取并校验 ticker 响应。

        成功时将 result.data 替换为 {"raw": dict, "normalized": str}，
        其余元信息透传，调用方可直接将该结果传给 success_result。
        """
        inst_id, error = self._resolve_inst_id(symbol)
        if error:
            return data_error_result(self.name, error)

        result = await self._request(
            "GET", "/api/v5/market/ticker", params={"instId": inst_id}
        )
        if not result.success:
            return result

        api_error = self._check_api_code(result)
        if api_error:
            return api_error

        payload_list = result.data.get("data") or []
        if not payload_list or not isinstance(payload_list[0], dict):
            return empty_result(self.name, f"ticker 返回为空: {inst_id}", raw=result.data)

        result.data = {"raw": payload_list[0], "normalized": normalize_symbol(symbol)}
        return result

    def _check_api_code(self, result: FetchResult) -> FetchResult | None:
        """校验 OKX 业务码，code != "0" 时返回失败 FetchResult，否则返回 None。"""
        raw = result.data
        if not isinstance(raw, dict):
            return parse_error_result(
                self.name, f"期望 dict，得到 {type(raw).__name__}", raw=raw
            )
        code = str(raw.get("code", ""))
        if code != "0":
            msg = raw.get("msg", "")
            error_type = (
                ErrorType.RATE_LIMIT if code == "50011"
                else ErrorType.AUTH_ERROR if code in ("50102", "50104", "50105")
                else ErrorType.SERVER_ERROR if code.startswith("5")
                else ErrorType.DATA_FORMAT
            )
            return FetchResult(
                success=False,
                error=f"OKX API error code={code}: {msg}",
                error_type=error_type,
                status_code=result.status_code,
                response_time_ms=result.response_time_ms,
                provider_name=self.name,
                fetch_time=result.fetch_time,
                raw_response=raw,
            )
        return None

    @staticmethod
    def _parse_candles(rows: object) -> list[Candle]:
        """解析 OKX candle 数组（倒序输入 -> 升序输出）。

        字段顺序：[0]ts [1]open [2]high [3]low [4]close [5]vol(张)
        [6]volCcy(币) [7]volCcyQuote(额) [8]confirm(0=进行中,1=已收盘)
        """
        if not isinstance(rows, list):
            return []

        candles: list[Candle] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            ts = ms_to_datetime(row[0])
            o, h, low, c = (safe_float(row[i]) for i in (1, 2, 3, 4))
            if ts is None or None in (o, h, low, c):
                continue
            # 现货 vol 为张数，volCcy 为币数量；以 volCcy 作为标准 volume
            volume = safe_float(row[6]) if len(row) > 6 else None
            if volume is None:
                volume = safe_float(row[5]) or 0.0
            quote_volume = safe_float(row[7]) if len(row) > 7 else None
            confirm = row[8] if len(row) > 8 else "1"
            candles.append(Candle(
                timestamp=ts, open=o, high=h, low=low, close=c,
                volume=volume, quote_volume=quote_volume,
                trades=None, is_closed=str(confirm) == "1",
            ))
        candles.reverse()  # OKX 返回倒序
        return candles

    @staticmethod
    def _filter_range(
        candles: list[Candle],
        start: datetime | None,
        end: datetime | None,
        limit: int,
    ) -> list[Candle]:
        """按时间范围过滤并截断到 limit（保留最近的）。"""
        if start:
            start_aligned = align_to_interval(start, "1m")
            candles = [c for c in candles if c.timestamp >= start_aligned]
        if end:
            candles = [c for c in candles if c.timestamp <= end]
        if len(candles) > limit:
            candles = candles[-limit:]
        return candles

    @staticmethod
    def _parse_book_levels(levels: object, depth: int) -> list[list[float]] | None:
        """解析订单簿档位 [[price, qty, orders, sz], ...]。"""
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


__all__ = ["OKXProvider"]
