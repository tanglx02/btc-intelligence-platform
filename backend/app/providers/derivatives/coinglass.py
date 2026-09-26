"""CoinglassProvider — Coinglass 全市场衍生品聚合数据源。

API: https://open-api.coinglass.com/public/v2/...
鉴权：coinglass 请求头（COINGLASS_API_KEY），需要订阅计划。
无 Key / 权限不足返回 AUTH_ERROR，由 Failover 切换到 BinanceFutures 等。

覆盖：
- 资金费率（多交易所聚合）: /funding/v2/home
- 未平仓合约量（全网）: /open_interest/v2/home 及 symbol 明细
- 清算数据（全网聚合）: /liquidation_info/v2/home
- 多空比: /long_short/v2/home
- 基差: /futures/v2/symbol/{symbol}
- 溢价指数: /futures/v2/premiumIndex
"""

from datetime import datetime
from typing import Any

from app.providers.base.derivative_provider import BaseDerivativeProvider
from app.providers.base.payloads import derivative_payload, from_unix, unsupported_result
from app.providers.base.types import (
    ErrorType,
    FetchResult,
    ProviderMetadata,
    QualityStatus,
)


def _normalize_symbol(symbol: str) -> str:
    """'BTC/USDT' -> 'BTC'；'BTCUSDT' -> 'BTC'（Coinglass 使用币本位符号）。"""
    base = symbol.replace("/USDT", "").replace("-USDT", "").replace("USDT", "")
    return base.upper() or "BTC"


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ts(value: Any) -> datetime | None:
    """毫秒/秒时间戳 -> naive UTC datetime。"""
    num = _to_float(value)
    if not num:
        return None
    return from_unix(num / 1000 if num > 1e11 else num)


class CoinglassProvider(BaseDerivativeProvider):
    """Coinglass 全市场衍生品聚合 Provider。"""

    # ---- 生命周期 ----

    async def health_check(self) -> bool:
        """连通性探测：请求资金费率聚合端点。"""
        data, error, _raw = await self._get_public("/funding/v2/home")
        return error is None and data is not None

    def get_metadata(self) -> ProviderMetadata:
        if self._metadata is None:
            self._metadata = ProviderMetadata(
                name=self.name,
                category=self.category,
                description="Coinglass 全市场衍生品聚合数据（资金费率/OI/清算/多空比）",
                supported_symbols=self._config.supported_symbols or ["BTC/USDT"],
                documentation_url="https://docs.coinglass.com/",
            )
        return self._metadata

    def get_supported_data_types(self) -> list[str]:
        return [
            "funding_rate", "open_interest", "liquidation",
            "long_short_ratio", "basis", "premium",
        ]

    # ---- 内部工具 ----

    async def _get_public(
        self, path: str, params: dict[str, Any] | None = None
    ) -> tuple[Any | None, str | None, FetchResult | None]:
        """调用 Coinglass public/v2 API 并解包 data 字段。

        Returns:
            (data, error, raw_result)
        """
        if not self._config.api_key:
            return None, "COINGLASS_API_KEY not configured", None

        result = await self._request(
            "GET",
            f"/public/v2{path}",
            params=params,
            headers={"coinglass": self._config.api_key, "Accept": "application/json"},
        )
        if not result.success:
            if result.status_code in (401, 403):
                result.error_type = ErrorType.AUTH_ERROR
                result.error = f"Coinglass plan/auth restriction: {result.error}"
            return None, result.error or "fetch failed", result

        body = result.data
        if isinstance(body, dict):
            # Coinglass 响应格式：{success, code, msg, data}
            code = body.get("code")
            if body.get("success") is False or (code is not None and str(code) not in ("0", "200")):
                msg = body.get("msg") or body.get("message") or "api error"
                return None, f"Coinglass API error: {msg}", result
            return body.get("data"), None, result
        return body, None, result

    @staticmethod
    def _find_symbol_row(data: Any, symbol: str) -> dict[str, Any] | None:
        """从聚合列表中找到匹配 symbol 的行。"""
        if not isinstance(data, list):
            return None
        target = symbol.upper()
        for row in data:
            if not isinstance(row, dict):
                continue
            for key in ("symbol", "pair", "baseAsset"):
                value = row.get(key)
                if isinstance(value, str) and value.upper() in (target, f"{target}/USDT", f"{target}USDT"):
                    return row
        return None

    def _auth_error(self, error: str | None) -> FetchResult:
        error_type = (
            ErrorType.AUTH_ERROR
            if error and ("not configured" in error or "auth" in error.lower())
            else ErrorType.NETWORK_ERROR
        )
        return FetchResult(
            success=False,
            error=error or "coinglass request failed",
            error_type=error_type,
            provider_name=self.name,
        )

    # ---- 资金费率 ----

    async def get_funding_rate(
        self, symbol: str = "BTC/USDT", limit: int = 1
    ) -> FetchResult:
        """获取多交易所资金费率聚合。"""
        if limit > 1:
            # 历史序列需要 /funding/v3/history（更高订阅层级）
            return unsupported_result(self.name, "funding_rate_history")

        base = _normalize_symbol(symbol)
        data, error, raw = await self._get_public("/funding/v2/home")
        if error:
            return self._auth_error(error)

        row = self._find_symbol_row(data, base)
        if not row:
            return FetchResult(
                success=False,
                error=f"No funding data for '{symbol}'",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
            )

        ts = _ts(row.get("u") or row.get("updateTime") or row.get("timestamp"))
        exchanges = row.get("rates") or row.get("list") or []
        return FetchResult(
            success=True,
            data=derivative_payload(
                "funding",
                symbol,
                funding_rate=_to_float(row.get("avgRate") or row.get("fundingRate")),
                max_rate=_to_float(row.get("maxRate")),
                min_rate=_to_float(row.get("minRate")),
                next_funding_time=(
                    _ts(row.get("nextFundingTime")).isoformat()
                    if _ts(row.get("nextFundingTime")) else None
                ),
                timestamp=ts,
                source=self.name,
                exchange_rates=[
                    {
                        "exchange": e.get("exchange"),
                        "rate": _to_float(e.get("rate")),
                    }
                    for e in exchanges
                    if isinstance(e, dict)
                ] or None,
            ),
            provider_name=self.name,
            fetch_time=datetime.utcnow(),
            observation_time=ts,
            quality_status=QualityStatus.VERIFIED,
            raw_response=raw.raw_response if raw else None,
            status_code=raw.status_code if raw else None,
            response_time_ms=raw.response_time_ms if raw else 0.0,
            metadata={"aggregated": True, "unit": "rate"},
        )

    # ---- 未平仓合约量 ----

    async def get_open_interest(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取全网未平仓合约量（USD + BTC）。"""
        base = _normalize_symbol(symbol)
        data, error, raw = await self._get_public("/open_interest/v2/home")
        if error:
            return self._auth_error(error)

        # home 端点：{usdOpenInterest: {...h24}, btcOpenInterest: {...h24}, symbolOpenInterestList: [...]}
        oi_usd: float | None = None
        oi_btc: float | None = None
        if isinstance(data, dict):
            usd_oi = data.get("usdOpenInterest")
            btc_oi = data.get("btcOpenInterest")
            if isinstance(usd_oi, dict):
                oi_usd = _to_float(usd_oi.get("h24"))
            elif usd_oi is not None:
                oi_usd = _to_float(usd_oi)
            if isinstance(btc_oi, dict):
                oi_btc = _to_float(btc_oi.get("h24"))
            elif btc_oi is not None:
                oi_btc = _to_float(btc_oi)

            # 该 symbol 的 OI 明细
            symbol_row = self._find_symbol_row(
                data.get("symbolOpenInterestList"), base
            )
            if symbol_row:
                oi_usd = _to_float(symbol_row.get("usdOpenInterest")) or oi_usd
                oi_btc = _to_float(symbol_row.get("btcOpenInterest")) or oi_btc

        if oi_usd is None and oi_btc is None:
            return FetchResult(
                success=False,
                error="No open interest data in response",
                error_type=ErrorType.DATA_FORMAT,
                provider_name=self.name,
            )

        ts = datetime.utcnow()
        return FetchResult(
            success=True,
            data=derivative_payload(
                "open_interest",
                symbol,
                open_interest=oi_btc,
                open_interest_usd=oi_usd,
                timestamp=ts,
                source=self.name,
            ),
            provider_name=self.name,
            fetch_time=ts,
            observation_time=ts,
            quality_status=QualityStatus.VERIFIED,
            raw_response=raw.raw_response if raw else None,
            status_code=raw.status_code if raw else None,
            response_time_ms=raw.response_time_ms if raw else 0.0,
            metadata={"aggregated": True, "unit": "btc/usd"},
        )

    # ---- 清算 ----

    async def get_liquidation(
        self, symbol: str = "BTC/USDT", period: str = "1h"
    ) -> FetchResult:
        """获取全网清算数据（Coinglass 聚合，最近 4h 窗口）。"""
        base = _normalize_symbol(symbol)
        data, error, raw = await self._get_public("/liquidation_info/v2/home")
        if error:
            return self._auth_error(error)

        row = self._find_symbol_row(data, base) if isinstance(data, list) else (
            data if isinstance(data, dict) else None
        )
        if not row:
            return FetchResult(
                success=False,
                error=f"No liquidation data for '{symbol}'",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
            )

        ts = _ts(row.get("ts") or row.get("u") or row.get("createTime"))
        return FetchResult(
            success=True,
            data=derivative_payload(
                "liquidation",
                symbol,
                long_liquidation_vol=_to_float(row.get("buyAmount") or row.get("longVol")),
                short_liquidation_vol=_to_float(row.get("sellAmount") or row.get("shortVol")),
                long_liquidation_usd=_to_float(row.get("buyAmountUsd") or row.get("longUsd")),
                short_liquidation_usd=_to_float(row.get("sellAmountUsd") or row.get("shortUsd")),
                timestamp=ts,
                source=self.name,
            ),
            provider_name=self.name,
            fetch_time=datetime.utcnow(),
            observation_time=ts or datetime.utcnow(),
            quality_status=QualityStatus.VERIFIED,
            raw_response=raw.raw_response if raw else None,
            status_code=raw.status_code if raw else None,
            response_time_ms=raw.response_time_ms if raw else 0.0,
            metadata={"aggregated": True, "period": period, "window": "4h", "unit": "usd"},
        )

    # ---- 多空比 ----

    async def get_long_short_ratio(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取全网多空比聚合。"""
        base = _normalize_symbol(symbol)
        data, error, raw = await self._get_public("/long_short/v2/home")
        if error:
            return self._auth_error(error)

        row = self._find_symbol_row(data, base)
        if not row:
            return FetchResult(
                success=False,
                error=f"No long/short data for '{symbol}'",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
            )

        ts = _ts(row.get("ts") or row.get("u") or row.get("createTime"))
        return FetchResult(
            success=True,
            data=derivative_payload(
                "long_short_ratio",
                symbol,
                long_short_ratio=_to_float(row.get("longRate") or row.get("longShortRatio")),
                long_ratio=_to_float(row.get("longRate")),
                short_ratio=_to_float(row.get("shortRate")),
                timestamp=ts,
                source=self.name,
            ),
            provider_name=self.name,
            fetch_time=datetime.utcnow(),
            observation_time=ts or datetime.utcnow(),
            quality_status=QualityStatus.VERIFIED,
            raw_response=raw.raw_response if raw else None,
            status_code=raw.status_code if raw else None,
            response_time_ms=raw.response_time_ms if raw else 0.0,
            metadata={"aggregated": True},
        )

    # ---- 基差与溢价 ----

    async def get_basis(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取基差（季度/永续合约价格 - 现货指数）。"""
        base = _normalize_symbol(symbol)
        data, error, raw = await self._get_public(f"/futures/v2/symbol/{base}")
        if error:
            return self._auth_error(error)

        if not isinstance(data, list) or not data:
            return FetchResult(
                success=False,
                error=f"No futures basis data for '{symbol}'",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
            )

        # 取首行（通常为永续或最近到期合约）
        row = data[0] if isinstance(data[0], dict) else {}
        ts = _ts(row.get("u") or row.get("updateTime"))
        return FetchResult(
            success=True,
            data=derivative_payload(
                "basis",
                symbol,
                basis=_to_float(row.get("basisRate") or row.get("basis")),
                basis_rate=_to_float(row.get("basisRate")),
                contract_price=_to_float(row.get("close") or row.get("price")),
                index_price=_to_float(row.get("indexPrice")),
                timestamp=ts,
                source=self.name,
            ),
            provider_name=self.name,
            fetch_time=datetime.utcnow(),
            observation_time=ts or datetime.utcnow(),
            quality_status=QualityStatus.VERIFIED,
            raw_response=raw.raw_response if raw else None,
            status_code=raw.status_code if raw else None,
            response_time_ms=raw.response_time_ms if raw else 0.0,
            metadata={"aggregated": True, "contract": row.get("pair") or row.get("symbol")},
        )

    async def get_premium(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取溢价指数。"""
        base = _normalize_symbol(symbol)
        data, error, raw = await self._get_public("/futures/v2/premiumIndex")
        if error:
            return self._auth_error(error)

        row = self._find_symbol_row(data, base) if isinstance(data, list) else None
        if not row and isinstance(data, dict):
            row = data
        if not row:
            return FetchResult(
                success=False,
                error=f"No premium index for '{symbol}'",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
            )

        ts = _ts(row.get("u") or row.get("updateTime"))
        premium = _to_float(row.get("premiumIndex") or row.get("premium"))
        return FetchResult(
            success=True,
            data=derivative_payload(
                "premium",
                symbol,
                premium_rate=premium,
                premium_rate_annualized=premium * 3 * 365 if premium is not None else None,
                mark_price=_to_float(row.get("markPrice")),
                timestamp=ts,
                source=self.name,
            ),
            provider_name=self.name,
            fetch_time=datetime.utcnow(),
            observation_time=ts or datetime.utcnow(),
            quality_status=QualityStatus.VERIFIED,
            raw_response=raw.raw_response if raw else None,
            status_code=raw.status_code if raw else None,
            response_time_ms=raw.response_time_ms if raw else 0.0,
            metadata={"aggregated": True, "unit": "rate"},
        )

    # ---- 主动买卖量 / CVD（需更高订阅层级端点）----

    async def get_taker_buy_sell(
        self, symbol: str = "BTC/USDT", interval: str = "1h"
    ) -> FetchResult:
        """Coinglass public/v2 无 taker 买卖量聚合端点。"""
        return unsupported_result(self.name, "taker_buy_sell")

    async def get_derivative_cvd(
        self, symbol: str = "BTC/USDT", interval: str = "1h"
    ) -> FetchResult:
        """Coinglass CVD 端点需专业订阅，此处标记不支持由 Failover 处理。"""
        return unsupported_result(self.name, "derivative_cvd")


__all__ = ["CoinglassProvider"]
