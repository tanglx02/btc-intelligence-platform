"""BinanceFuturesProvider — Binance USDT-M 合约衍生品数据源。

API: https://fapi.binance.com/（公共端点，无需 API Key）
覆盖：
- 资金费率（当前 / 历史 / 预测）: /fapi/v1/premiumIndex, /fapi/v1/fundingRate
- 未平仓合约量: /fapi/v1/openInterest, /futures/data/openInterestHist
- 多空比: /futures/data/globalLongShortAccountRatio
- 主动买卖量 / CVD: /futures/data/takerlongshortRatio
- 基差与溢价: premiumIndex（markPrice vs indexPrice）
- 清算: /fapi/v1/allForceOrders（现已要求签名鉴权，无 Key 时返回 AUTH_ERROR）
"""

from datetime import datetime
from typing import Any

from app.providers.base.derivative_provider import BaseDerivativeProvider
from app.providers.base.payloads import derivative_payload, from_unix
from app.providers.base.types import (
    ErrorType,
    FetchResult,
    ProviderMetadata,
    QualityStatus,
)

# period -> Binance interval 映射
_INTERVAL_MAP = {
    "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "2h": "2h", "4h": "4h",
    "6h": "6h", "12h": "12h", "1d": "1d",
}


def _normalize_symbol(symbol: str) -> str:
    """'BTC/USDT' / 'BTC-USDT' -> 'BTCUSDT'。"""
    return symbol.replace("/", "").replace("-", "").replace("_", "").upper()


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class BinanceFuturesProvider(BaseDerivativeProvider):
    """Binance USDT-M 合约衍生品 Provider（免费公共 API）。"""

    # ---- 生命周期 ----

    async def health_check(self) -> bool:
        """连通性探测：/fapi/v1/ping。"""
        result = await self._request("GET", "/fapi/v1/ping")
        return result.success

    def get_metadata(self) -> ProviderMetadata:
        if self._metadata is None:
            self._metadata = ProviderMetadata(
                name=self.name,
                category=self.category,
                description="Binance USDT-M 合约（资金费率/未平仓/多空比/主动买卖量）",
                supported_symbols=self._config.supported_symbols or ["BTC/USDT"],
                supported_intervals=["5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"],
                documentation_url="https://developers.binance.com/docs/derivatives/",
            )
        return self._metadata

    def get_supported_data_types(self) -> list[str]:
        return [
            "funding_rate", "open_interest", "long_short_ratio",
            "basis", "premium", "taker_buy_sell", "derivative_cvd",
        ]

    # ---- 资金费率 ----

    async def get_funding_rate(
        self, symbol: str = "BTC/USDT", limit: int = 1
    ) -> FetchResult:
        """获取资金费率。

        limit=1: 返回当前费率 + 预测费率（premiumIndex）；
        limit>1: 返回历史费率序列（fundingRate 端点）。
        """
        bsymbol = _normalize_symbol(symbol)

        if limit <= 1:
            result = await self._request(
                "GET", "/fapi/v1/premiumIndex", params={"symbol": bsymbol}
            )
            if not result.success:
                return result
            raw = result.data
            if not isinstance(raw, dict):
                return self._format_error(raw)

            return FetchResult(
                success=True,
                data=derivative_payload(
                    "funding",
                    symbol,
                    funding_rate=_to_float(raw.get("lastFundingRate")),
                    predicted_rate=_to_float(raw.get("interestRate")),
                    mark_price=_to_float(raw.get("markPrice")),
                    index_price=_to_float(raw.get("indexPrice")),
                    next_funding_time=from_unix(
                        (raw.get("nextFundingTime") or 0) / 1000
                    ).isoformat() if raw.get("nextFundingTime") else None,
                    timestamp=self._ts_from_ms(raw.get("time")),
                    source=self.name,
                ),
                provider_name=self.name,
                fetch_time=datetime.utcnow(),
                observation_time=self._ts_from_ms(raw.get("time")),
                quality_status=QualityStatus.VERIFIED,
                raw_response=result.raw_response,
                status_code=result.status_code,
                response_time_ms=result.response_time_ms,
                metadata={"exchange": "binance_futures", "unit": "rate"},
            )

        # 历史资金费率
        result = await self._request(
            "GET",
            "/fapi/v1/fundingRate",
            params={"symbol": bsymbol, "limit": min(limit, 1000)},
        )
        if not result.success:
            return result
        rows = result.data if isinstance(result.data, list) else []
        if not rows:
            return self._empty_error("funding rate history")

        series = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            ts = self._ts_from_ms(row.get("fundingTime"))
            series.append({
                "funding_rate": _to_float(row.get("fundingRate")),
                "timestamp": ts.isoformat() if ts else None,
            })

        last_ts = self._ts_from_ms(rows[-1].get("fundingTime")) if rows else None
        return FetchResult(
            success=True,
            data=derivative_payload(
                "funding", symbol, timestamp=last_ts, source=self.name, history=series
            ),
            provider_name=self.name,
            fetch_time=datetime.utcnow(),
            observation_time=last_ts,
            quality_status=QualityStatus.VERIFIED,
            raw_response=result.raw_response,
            status_code=result.status_code,
            response_time_ms=result.response_time_ms,
            metadata={"exchange": "binance_futures", "count": len(series)},
        )

    # ---- 未平仓合约量 ----

    async def get_open_interest(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取未平仓合约量（当前值 + USD 名义价值）。"""
        bsymbol = _normalize_symbol(symbol)

        oi_result = await self._request(
            "GET", "/fapi/v1/openInterest", params={"symbol": bsymbol}
        )
        if not oi_result.success:
            return oi_result
        oi_raw = oi_result.data
        if not isinstance(oi_raw, dict):
            return self._format_error(oi_raw)

        open_interest = _to_float(oi_raw.get("openInterest"))

        # 用标记价格折算 USD 名义价值（额外一次轻量请求）
        mark_price: float | None = None
        ticker = await self._request(
            "GET", "/fapi/v1/premiumIndex", params={"symbol": bsymbol}
        )
        if ticker.success and isinstance(ticker.data, dict):
            mark_price = _to_float(ticker.data.get("markPrice"))

        oi_usd = open_interest * mark_price if open_interest and mark_price else None
        ts = self._ts_from_ms(oi_raw.get("time"))

        return FetchResult(
            success=True,
            data=derivative_payload(
                "open_interest",
                symbol,
                open_interest=open_interest,
                open_interest_usd=oi_usd,
                mark_price=mark_price,
                timestamp=ts,
                source=self.name,
            ),
            provider_name=self.name,
            fetch_time=datetime.utcnow(),
            observation_time=ts,
            quality_status=QualityStatus.VERIFIED,
            raw_response=oi_result.raw_response,
            status_code=oi_result.status_code,
            response_time_ms=oi_result.response_time_ms,
            metadata={"exchange": "binance_futures", "unit": "btc"},
        )

    # ---- 清算 ----

    async def get_liquidation(
        self, symbol: str = "BTC/USDT", period: str = "1h"
    ) -> FetchResult:
        """获取清算数据。

        注意：/fapi/v1/allForceOrders 已要求签名鉴权（SIGNED），
        未配置 API Key/Secret 时将返回 AUTH_ERROR，由 Failover 切换到 Coinglass。
        """
        bsymbol = _normalize_symbol(symbol)
        result = await self._request(
            "GET", "/fapi/v1/allForceOrders", params={"symbol": bsymbol, "limit": 100}
        )
        if not result.success:
            if result.status_code in (400, 401) and "signature" in (result.error or "").lower():
                return FetchResult(
                    success=False,
                    error=(
                        "Binance allForceOrders requires signed request; "
                        "configure API key/secret or use Coinglass for liquidations"
                    ),
                    error_type=ErrorType.AUTH_ERROR,
                    status_code=result.status_code,
                    provider_name=self.name,
                )
            return result

        rows = result.data if isinstance(result.data, list) else []
        long_vol = short_vol = 0.0
        long_count = short_count = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            qty = _to_float(row.get("origQty")) or 0.0
            if row.get("side") == "SELL":  # 多头被清算
                long_vol += qty
                long_count += 1
            elif row.get("side") == "BUY":  # 空头被清算
                short_vol += qty
                short_count += 1

        return FetchResult(
            success=True,
            data=derivative_payload(
                "liquidation",
                symbol,
                long_liquidation_vol=long_vol,
                short_liquidation_vol=short_vol,
                long_liquidation_count=long_count,
                short_liquidation_count=short_count,
                timestamp=datetime.utcnow(),
                source=self.name,
            ),
            provider_name=self.name,
            fetch_time=datetime.utcnow(),
            observation_time=datetime.utcnow(),
            quality_status=QualityStatus.ESTIMATED,  # 仅统计返回窗口内的订单
            raw_response=result.raw_response,
            status_code=result.status_code,
            response_time_ms=result.response_time_ms,
            metadata={"exchange": "binance_futures", "period": period, "unit": "btc"},
        )

    # ---- 多空比 ----

    async def get_long_short_ratio(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取全网账户多空比（最近一个 5m 数据点）。"""
        bsymbol = _normalize_symbol(symbol)
        result = await self._request(
            "GET",
            "/futures/data/globalLongShortAccountRatio",
            params={"symbol": bsymbol, "period": "5m", "limit": 1},
        )
        if not result.success:
            return result
        rows = result.data if isinstance(result.data, list) else []
        if not rows or not isinstance(rows[0], dict):
            return self._empty_error("long/short ratio")

        row = rows[-1]
        ts = self._ts_from_ms(row.get("timestamp"))
        return FetchResult(
            success=True,
            data=derivative_payload(
                "long_short_ratio",
                symbol,
                long_short_ratio=_to_float(row.get("longShortRatio")),
                long_account=_to_float(row.get("longAccount")),
                short_account=_to_float(row.get("shortAccount")),
                timestamp=ts,
                source=self.name,
            ),
            provider_name=self.name,
            fetch_time=datetime.utcnow(),
            observation_time=ts,
            quality_status=QualityStatus.VERIFIED,
            raw_response=result.raw_response,
            status_code=result.status_code,
            response_time_ms=result.response_time_ms,
            metadata={"exchange": "binance_futures", "period": "5m"},
        )

    # ---- 基差与溢价 ----

    async def get_basis(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取基差（永续标记价格 - 指数价格）。"""
        bsymbol = _normalize_symbol(symbol)
        result = await self._request(
            "GET", "/fapi/v1/premiumIndex", params={"symbol": bsymbol}
        )
        if not result.success:
            return result
        raw = result.data
        if not isinstance(raw, dict):
            return self._format_error(raw)

        mark = _to_float(raw.get("markPrice"))
        index = _to_float(raw.get("indexPrice"))
        basis = mark - index if mark is not None and index is not None else None
        ts = self._ts_from_ms(raw.get("time"))

        return FetchResult(
            success=True,
            data=derivative_payload(
                "basis",
                symbol,
                basis=basis,
                mark_price=mark,
                index_price=index,
                timestamp=ts,
                source=self.name,
            ),
            provider_name=self.name,
            fetch_time=datetime.utcnow(),
            observation_time=ts,
            quality_status=QualityStatus.VERIFIED,
            raw_response=result.raw_response,
            status_code=result.status_code,
            response_time_ms=result.response_time_ms,
            metadata={"exchange": "binance_futures", "unit": "usd"},
        )

    async def get_premium(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取溢价率（当期资金费率 + 年化）。"""
        funding = await self.get_funding_rate(symbol=symbol, limit=1)
        if not funding.success or not isinstance(funding.data, dict):
            return funding

        rate = funding.data.get("funding_rate")
        annualized = rate * 3 * 365 if rate is not None else None
        funding.data.update({
            "data_type": "premium",
            "premium_rate": rate,
            "premium_rate_annualized": annualized,
        })
        funding.metadata = {**funding.metadata, "unit": "rate"}
        return funding

    # ---- 主动买卖量 / CVD ----

    async def get_taker_buy_sell(
        self, symbol: str = "BTC/USDT", interval: str = "1h"
    ) -> FetchResult:
        """获取主动买卖量（takerlongshortRatio 端点，最近 2 个数据点）。"""
        bsymbol = _normalize_symbol(symbol)
        binance_interval = _INTERVAL_MAP.get(interval, "1h")
        result = await self._request(
            "GET",
            "/futures/data/takerlongshortRatio",
            params={"symbol": bsymbol, "period": binance_interval, "limit": 2},
        )
        if not result.success:
            return result
        rows = result.data if isinstance(result.data, list) else []
        if not rows or not isinstance(rows[-1], dict):
            return self._empty_error("taker buy/sell")

        row = rows[-1]
        ts = self._ts_from_ms(row.get("timestamp"))
        return FetchResult(
            success=True,
            data=derivative_payload(
                "taker",
                symbol,
                buy_vol=_to_float(row.get("buyVol")),
                sell_vol=_to_float(row.get("sellVol")),
                buy_sell_ratio=_to_float(row.get("buySellRatio")),
                timestamp=ts,
                source=self.name,
            ),
            provider_name=self.name,
            fetch_time=datetime.utcnow(),
            observation_time=ts,
            quality_status=QualityStatus.VERIFIED,
            raw_response=result.raw_response,
            status_code=result.status_code,
            response_time_ms=result.response_time_ms,
            metadata={"exchange": "binance_futures", "interval": binance_interval, "unit": "btc"},
        )

    async def get_derivative_cvd(
        self, symbol: str = "BTC/USDT", interval: str = "1h"
    ) -> FetchResult:
        """获取衍生品 CVD（对 taker buy-sell 差值做窗口累计）。"""
        bsymbol = _normalize_symbol(symbol)
        binance_interval = _INTERVAL_MAP.get(interval, "1h")
        result = await self._request(
            "GET",
            "/futures/data/takerlongshortRatio",
            params={"symbol": bsymbol, "period": binance_interval, "limit": 30},
        )
        if not result.success:
            return result
        rows = result.data if isinstance(result.data, list) else []
        if not rows:
            return self._empty_error("derivative CVD")

        cvd = 0.0
        series = []
        last_ts: datetime | None = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            buy = _to_float(row.get("buyVol")) or 0.0
            sell = _to_float(row.get("sellVol")) or 0.0
            cvd += buy - sell
            ts = self._ts_from_ms(row.get("timestamp"))
            last_ts = ts or last_ts
            series.append({
                "cvd": cvd,
                "buy_vol": buy,
                "sell_vol": sell,
                "timestamp": ts.isoformat() if ts else None,
            })

        return FetchResult(
            success=True,
            data=derivative_payload(
                "cvd",
                symbol,
                cvd=cvd,
                window=interval,
                timestamp=last_ts,
                source=self.name,
                history=series,
            ),
            provider_name=self.name,
            fetch_time=datetime.utcnow(),
            observation_time=last_ts,
            quality_status=QualityStatus.ESTIMATED,  # 窗口内累计，非全历史 CVD
            raw_response=result.raw_response,
            status_code=result.status_code,
            response_time_ms=result.response_time_ms,
            metadata={"exchange": "binance_futures", "interval": binance_interval, "unit": "btc"},
        )

    # ---- 工具 ----

    @staticmethod
    def _ts_from_ms(ms: Any) -> datetime | None:
        """毫秒时间戳 -> naive UTC datetime。"""
        value = _to_float(ms)
        if not value:
            return None
        return from_unix(value / 1000)

    def _empty_error(self, what: str) -> FetchResult:
        return FetchResult(
            success=False,
            error=f"Empty response for {what}",
            error_type=ErrorType.EMPTY_RESPONSE,
            provider_name=self.name,
        )

    def _format_error(self, raw: Any) -> FetchResult:
        return FetchResult(
            success=False,
            error=f"Unexpected response format: {str(raw)[:200]}",
            error_type=ErrorType.DATA_FORMAT,
            provider_name=self.name,
        )


__all__ = ["BinanceFuturesProvider"]
