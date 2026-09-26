"""CoinglassOptionsProvider — Coinglass 期权聚合数据源。

API: https://open-api.coinglass.com/public/v2/options/...
鉴权与 CoinglassProvider 相同（coinglass 请求头），因此直接复用其请求管道。

覆盖：
- 期权成交量（多交易所聚合）: /options/volume
- 到期日数据: /options/expire
- 期权 OI / IV / Put-Call / Skew / GEX：public/v2 无对应免费端点，
  标记为 unsupported（由 Failover 切换到 Deribit）
"""

from datetime import datetime
from typing import Any

from app.providers.base.payloads import from_unix, options_payload, unsupported_result
from app.providers.base.types import (
    ErrorType,
    FetchResult,
    ProviderMetadata,
    QualityStatus,
)
from app.providers.derivatives.coinglass import CoinglassProvider


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ts(value: Any) -> datetime | None:
    num = _to_float(value)
    if not num:
        return None
    return from_unix(num / 1000 if num > 1e11 else num)


class CoinglassOptionsProvider(CoinglassProvider):
    """Coinglass 期权聚合 Provider（复用 Coinglass 请求/鉴权管道）。"""

    # 注册名（与 providers.yaml 配置 key 对齐）
    provider_key = "coinglass_options"

    # ---- 生命周期 ----

    async def health_check(self) -> bool:
        """连通性探测：请求期权成交量聚合端点。"""
        data, error, _raw = await self._get_public("/options/volume")
        return error is None and data is not None

    def get_metadata(self) -> ProviderMetadata:
        if self._metadata is None:
            self._metadata = ProviderMetadata(
                name=self.name,
                category=self.category,
                description="Coinglass 期权聚合数据（成交量/到期日）",
                supported_symbols=["BTC", "ETH"],
                documentation_url="https://docs.coinglass.com/",
            )
        return self._metadata

    def get_supported_data_types(self) -> list[str]:
        return ["options_volume", "expiration_data"]

    # ---- 不支持的端点（由 Deribit 提供）----

    async def get_options_oi(self, symbol: str = "BTC") -> FetchResult:
        return unsupported_result(self.name, "options_oi")

    async def get_implied_volatility(self, symbol: str = "BTC") -> FetchResult:
        return unsupported_result(self.name, "implied_volatility")

    async def get_put_call_ratio(self, symbol: str = "BTC") -> FetchResult:
        return unsupported_result(self.name, "put_call_ratio")

    async def get_skew(self, symbol: str = "BTC") -> FetchResult:
        return unsupported_result(self.name, "skew")

    async def get_gamma_exposure(self, symbol: str = "BTC") -> FetchResult:
        return unsupported_result(self.name, "gamma_exposure")

    # ---- 支持的端点 ----

    async def get_options_volume(self, symbol: str = "BTC") -> FetchResult:
        """全网期权成交量聚合（多交易所）。"""
        base = symbol.upper()
        data, error, raw = await self._get_public(
            "/options/volume", params={"symbol": base}
        )
        if error:
            return FetchResult(
                success=False,
                error=error,
                error_type=(
                    ErrorType.AUTH_ERROR
                    if "not configured" in error or "auth" in error.lower()
                    else ErrorType.NETWORK_ERROR
                ),
                provider_name=self.name,
            )

        exchanges: list[dict[str, Any]] = []
        total_vol = 0.0
        rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        ts: datetime | None = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            vol = _to_float(
                row.get("volume") or row.get("usdVolume") or row.get("vol")
            ) or 0.0
            total_vol += vol
            ts = _ts(row.get("u") or row.get("timestamp") or row.get("createTime")) or ts
            exchanges.append({
                "exchange": row.get("exchange") or row.get("exchangeName"),
                "volume_usd": vol,
            })

        if not exchanges:
            return FetchResult(
                success=False,
                error=f"No options volume data for '{symbol}'",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
            )

        fetch_ts = datetime.utcnow()
        return FetchResult(
            success=True,
            data=options_payload(
                "volume",
                symbol,
                volume=total_vol,
                exchanges=exchanges,
                timestamp=ts or fetch_ts,
                source=self.name,
            ),
            provider_name=self.name,
            fetch_time=fetch_ts,
            observation_time=ts or fetch_ts,
            quality_status=QualityStatus.VERIFIED,
            raw_response=raw.raw_response if raw else None,
            status_code=raw.status_code if raw else None,
            response_time_ms=raw.response_time_ms if raw else 0.0,
            metadata={"aggregated": True, "unit": "usd"},
        )

    async def get_expiration_data(self, symbol: str = "BTC") -> FetchResult:
        """期权到期日数据聚合。"""
        base = symbol.upper()
        data, error, raw = await self._get_public(
            "/options/expire", params={"symbol": base}
        )
        if error:
            return FetchResult(
                success=False,
                error=error,
                error_type=(
                    ErrorType.AUTH_ERROR
                    if "not configured" in error or "auth" in error.lower()
                    else ErrorType.NETWORK_ERROR
                ),
                provider_name=self.name,
            )

        rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        expirations: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            expiry_ts = _ts(row.get("timestamp") or row.get("expireTime") or row.get("u"))
            expirations.append({
                "expiry": expiry_ts.isoformat() if expiry_ts else row.get("date"),
                "open_interest_usd": _to_float(row.get("openInterest") or row.get("oi")),
                "volume_usd": _to_float(row.get("volume") or row.get("vol")),
                "exchange": row.get("exchange") or row.get("exchangeName"),
            })

        if not expirations:
            return FetchResult(
                success=False,
                error=f"No options expiration data for '{symbol}'",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
            )

        fetch_ts = datetime.utcnow()
        return FetchResult(
            success=True,
            data=options_payload(
                "expiration",
                symbol,
                expirations=expirations,
                timestamp=fetch_ts,
                source=self.name,
            ),
            provider_name=self.name,
            fetch_time=fetch_ts,
            observation_time=fetch_ts,
            quality_status=QualityStatus.VERIFIED,
            raw_response=raw.raw_response if raw else None,
            status_code=raw.status_code if raw else None,
            response_time_ms=raw.response_time_ms if raw else 0.0,
            metadata={"aggregated": True, "count": len(expirations)},
        )


__all__ = ["CoinglassOptionsProvider"]
