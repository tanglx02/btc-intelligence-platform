"""SoSoValueProvider — SoSoValue ETF 资金流与持仓数据源。

API: POST {base_url}/asset/etf-asset（base_url 默认 https://api.sosovalue.com，
可通过配置改为 https://api.sosovalue.xyz）
鉴权：apiKey 请求头（SOSOV_VALUE_API_KEY）。

响应包含：
- metadata.sumNetFlow / sumDailyNetFlow / sumDailyNetFlowVol7d / Vol30d
- list[]: 各 ETF 的 ticker、dailyNetFlow、sumDailyNetFlow、
  currentHoldingBtc、dailyTotalNetAsset、volume 等

字段解析做了多别名兼容（SoSoValue 非公开文档，字段名可能调整）。
响应内存缓存 30 分钟。
"""

import time
from datetime import datetime, timedelta
from typing import Any

from loguru import logger

from app.providers.base.etf_provider import BaseETFProvider
from app.providers.base.payloads import (
    etf_flow_payload,
    etf_holdings_payload,
    from_unix,
)
from app.providers.base.types import (
    ErrorType,
    FetchResult,
    ProviderMetadata,
    QualityStatus,
)
from app.utils.datetime_utils import as_naive_utc, utcnow

_CACHE_TTL = 1800.0
_PERIOD_KEYS = {
    "1d": "sumDailyNetFlow",
    "7d": "sumDailyNetFlowVol7d",
    "30d": "sumDailyNetFlowVol30d",
    "90d": None,  # 需自行累加 list 历史
}


def _first_key(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """按别名顺序取第一个存在且非空的字段值。"""
    for key in keys:
        if key in item and item[key] is not None:
            return item[key]
    return None


def _to_float(value: Any) -> float | None:
    """安全转 float。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class SoSoValueProvider(BaseETFProvider):
    """SoSoValue 美国现货 BTC ETF Provider。"""

    # 注册名（与 providers.yaml 配置 key 对齐；snake_case 推导会产生 so_so_value）
    provider_key = "sosovalue"

    def __init__(self, config: Any):
        super().__init__(config)
        self._cache: dict[str, Any] | None = None

    # ---- 生命周期 ----

    async def health_check(self) -> bool:
        """连通性探测：请求 ETF 资产数据。"""
        payload, error = await self._get_etf_assets()
        return error is None and bool(payload)

    def get_metadata(self) -> ProviderMetadata:
        if self._metadata is None:
            self._metadata = ProviderMetadata(
                name=self.name,
                category=self.category,
                description="SoSoValue 美国现货 BTC ETF 资金流与持仓",
                supported_symbols=["BTC"],
                data_coverage_start=datetime(2024, 1, 11),
                documentation_url="https://sosovalue.com/assets/etf/us-btc-spot",
            )
        return self._metadata

    def get_supported_data_types(self) -> list[str]:
        return ["daily_flow", "net_flow", "cumulative_flow", "holdings", "all_funds_flow"]

    # ---- 内部工具 ----

    def _headers(self) -> dict[str, str]:
        return {
            "apiKey": self._config.api_key or "",
            "Content-Type": "application/json",
        }

    async def _get_etf_assets(self) -> tuple[dict[str, Any] | None, str | None]:
        """获取 ETF 资产数据（带内存缓存）。

        Returns:
            (data, error)
        """
        if not self._config.api_key:
            return None, "SOSOV_VALUE_API_KEY not configured"

        now = time.monotonic()
        if self._cache and now - self._cache["cached_at"] < _CACHE_TTL:
            return self._cache["data"], None

        result = await self._request(
            "POST",
            "/asset/etf-asset",
            json={"type": "us-btc-spot", "isHistory": True},
            headers=self._headers(),
        )
        if not result.success:
            if result.status_code in (401, 403):
                result.error_type = ErrorType.AUTH_ERROR
            return None, result.error or "fetch failed"

        data = result.data if isinstance(result.data, dict) else None
        if not data:
            return None, "unexpected response format (expected JSON object)"

        self._cache = {"data": data, "cached_at": now}
        logger.debug(f"[{self.name}] ETF asset data refreshed")
        return data, None

    @staticmethod
    def _extract_fund_list(data: dict[str, Any]) -> list[dict[str, Any]]:
        """提取基金列表（兼容多种响应包裹层级）。"""
        for container_key in ("list", "data", "result"):
            container = data.get(container_key)
            if isinstance(container, list):
                return [item for item in container if isinstance(item, dict)]
            if isinstance(container, dict):
                inner = container.get("list")
                if isinstance(inner, list):
                    return [item for item in inner if isinstance(item, dict)]
        return []

    @staticmethod
    def _fund_fields(item: dict[str, Any]) -> dict[str, Any]:
        """从基金条目提取标准化字段（多别名兼容）。"""
        ticker = _first_key(item, ("ticker", "symbol", "code"))
        date_raw = _first_key(item, ("date", "timestamp", "ts", "time"))
        if isinstance(date_raw, (int, float)):
            # SoSoValue 时间戳为毫秒
            date = from_unix(date_raw / 1000 if date_raw > 1e11 else date_raw)
        else:
            date = None
        return {
            "ticker": str(ticker).upper() if ticker else "UNKNOWN",
            "fund_name": _first_key(item, ("fundName", "name", "fullName")),
            "date": date,
            "daily_net_flow": _to_float(_first_key(item, ("dailyNetFlow", "netFlow", "dailyFlow"))),
            "cumulative_flow": _to_float(
                _first_key(item, ("sumDailyNetFlow", "cumulativeNetFlow", "totalNetFlow"))
            ),
            "holdings_btc": _to_float(
                _first_key(item, ("currentHoldingBtc", "holdingBtc", "holdingsBtc"))
            ),
            "aum_usd": _to_float(
                _first_key(item, ("dailyTotalNetAsset", "totalNetAsset", "aum", "netAsset"))
            ),
            "volume_usd": _to_float(_first_key(item, ("volume", "dailyVolume", "turnover"))),
            "price": _to_float(_first_key(item, ("price", "latestPrice", "close"))),
            "nav": _to_float(_first_key(item, ("nav", "netAssetValue"))),
        }

    # ---- 基类接口实现 ----

    async def get_daily_flow(self, date: datetime | None = None) -> FetchResult:
        """获取指定日期各 ETF 每日净流入/流出。"""
        data, error = await self._get_etf_assets()
        if error or not data:
            return FetchResult(
                success=False,
                error=error or "no data",
                error_type=(
                    ErrorType.AUTH_ERROR if error and "not configured" in error
                    else ErrorType.NETWORK_ERROR
                ),
                provider_name=self.name,
            )

        funds = [self._fund_fields(item) for item in self._extract_fund_list(data)]
        funds = [f for f in funds if f["daily_net_flow"] is not None]
        if date is not None:
            target = date.replace(hour=0, minute=0, second=0, microsecond=0)
            dated = [f for f in funds if f["date"] and f["date"].date() == target.date()]
            funds = dated or funds

        if not funds:
            return FetchResult(
                success=False,
                error="No ETF fund flow entries in response",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
            )

        payload = [
            etf_flow_payload(
                f["ticker"],
                "net",
                f["daily_net_flow"],
                date=f["date"],
                fund_name=f["fund_name"],
                cumulative_usd=f["cumulative_flow"],
                source=self.name,
            )
            for f in funds
        ]
        return FetchResult(
            success=True,
            data=payload,
            provider_name=self.name,
            fetch_time=utcnow(),
            observation_time=funds[0]["date"],
            quality_status=QualityStatus.VERIFIED,
            metadata={"unit": "usd", "funds": len(payload)},
        )

    async def get_net_flow(
        self, period: str = "7d", end_date: datetime | None = None
    ) -> FetchResult:
        """获取指定周期全市场净流量（优先使用 metadata 汇总字段）。"""
        data, error = await self._get_etf_assets()
        if error or not data:
            return FetchResult(
                success=False,
                error=error or "no data",
                error_type=ErrorType.NETWORK_ERROR,
                provider_name=self.name,
            )

        if period not in _PERIOD_KEYS:
            return FetchResult(
                success=False,
                error=f"Unsupported period '{period}' (use 1d/7d/30d/90d)",
                error_type=ErrorType.DATA_FORMAT,
                provider_name=self.name,
            )

        # 从 metadata / data 汇总字段读取
        metadata_obj = data.get("metadata") if isinstance(data.get("metadata"), dict) else data
        net_value: float | None = None
        meta_key = _PERIOD_KEYS[period]
        if meta_key:
            net_value = _to_float(metadata_obj.get(meta_key))

        if net_value is None:
            # 回退：对基金列表的 daily_net_flow 求和（仅 1d 精确）
            days = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}[period]
            funds = [self._fund_fields(i) for i in self._extract_fund_list(data)]
            if end_date:
                # end_date 可能来自 Service/引擎（aware UTC）；f["date"] 为 naive UTC，
                # 比较前归一化避免 naive/aware 混算 TypeError
                end_date = as_naive_utc(end_date)
                boundary = end_date.replace(hour=0, minute=0, second=0, microsecond=0)
                funds = [f for f in funds if f["date"] and f["date"] <= boundary]
            cutoff = as_naive_utc(utcnow()) - timedelta(days=days)
            recent = [
                f["daily_net_flow"]
                for f in funds
                if f["daily_net_flow"] is not None and (f["date"] is None or f["date"] >= cutoff)
            ]
            if not recent:
                return FetchResult(
                    success=False,
                    error=f"Cannot compute net flow for period '{period}'",
                    error_type=ErrorType.EMPTY_RESPONSE,
                    provider_name=self.name,
                )
            net_value = sum(recent)

        payload = etf_flow_payload(
            "TOTAL",
            "net",
            net_value,
            date=end_date,
            period=period,
            fund_name="All US Spot BTC ETFs",
            source=self.name,
        )
        return FetchResult(
            success=True,
            data=payload,
            provider_name=self.name,
            fetch_time=utcnow(),
            observation_time=end_date,
            quality_status=QualityStatus.VERIFIED,
            metadata={"unit": "usd", "period": period},
        )

    async def get_cumulative_flow(self, start: datetime, end: datetime) -> FetchResult:
        """获取累计流量序列（按日聚合各基金 daily_net_flow 的 running sum）。"""
        data, error = await self._get_etf_assets()
        if error or not data:
            return FetchResult(
                success=False,
                error=error or "no data",
                error_type=ErrorType.NETWORK_ERROR,
                provider_name=self.name,
            )

        funds = [self._fund_fields(i) for i in self._extract_fund_list(data)]
        # 按日期聚合
        daily: dict[datetime, float] = {}
        for f in funds:
            if f["date"] and f["daily_net_flow"] is not None:
                day = f["date"].replace(hour=0, minute=0, second=0, microsecond=0)
                daily[day] = daily.get(day, 0.0) + f["daily_net_flow"]

        # start/end 可能来自 Service/引擎（aware UTC）；daily 键为 naive UTC，归一化后比较
        start_day = as_naive_utc(start).replace(hour=0, minute=0, second=0, microsecond=0)
        end_day = as_naive_utc(end).replace(hour=0, minute=0, second=0, microsecond=0)
        baseline = sum(v for d, v in daily.items() if d < start_day)

        cumulative = baseline
        payload: list[dict[str, Any]] = []
        for day in sorted(d for d in daily if start_day <= d <= end_day):
            cumulative += daily[day]
            payload.append(
                etf_flow_payload(
                    "TOTAL",
                    "cumulative",
                    daily[day],
                    date=day,
                    cumulative_usd=cumulative,
                    fund_name="All US Spot BTC ETFs",
                    source=self.name,
                )
            )

        if not payload:
            return FetchResult(
                success=False,
                error=f"No ETF flow rows in [{start_day}, {end_day}]",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
            )

        return FetchResult(
            success=True,
            data=payload,
            provider_name=self.name,
            fetch_time=utcnow(),
            observation_time=max(daily.keys()) if daily else None,
            quality_status=QualityStatus.VERIFIED,
            metadata={"unit": "usd", "baseline_usd": baseline, "points": len(payload)},
        )

    async def get_holdings(self, date: datetime | None = None) -> FetchResult:
        """获取各 ETF 持仓量（BTC）与 AUM。"""
        data, error = await self._get_etf_assets()
        if error or not data:
            return FetchResult(
                success=False,
                error=error or "no data",
                error_type=ErrorType.NETWORK_ERROR,
                provider_name=self.name,
            )

        funds = [self._fund_fields(i) for i in self._extract_fund_list(data)]
        held = [f for f in funds if f["holdings_btc"] is not None or f["aum_usd"] is not None]
        if not held:
            return FetchResult(
                success=False,
                error="No holdings fields in response",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
            )

        payload = [
            etf_holdings_payload(
                f["ticker"],
                holdings_btc=f["holdings_btc"],
                aum_usd=f["aum_usd"],
                nav=f["nav"],
                price=f["price"],
                fund_name=f["fund_name"],
                date=f["date"] or date,
                source=self.name,
            )
            for f in held
        ]
        total_btc = sum(f["holdings_btc"] or 0.0 for f in held)
        return FetchResult(
            success=True,
            data=payload,
            provider_name=self.name,
            fetch_time=utcnow(),
            observation_time=date,
            quality_status=QualityStatus.VERIFIED,
            metadata={"unit_btc": "btc", "total_holdings_btc": total_btc, "funds": len(payload)},
        )

    async def get_all_funds_flow(self, date: datetime | None = None) -> FetchResult:
        """获取所有 ETF 基金的分基金流量明细（dict[ticker, payload]）。"""
        result = await self.get_daily_flow(date=date)
        if not result.success or not isinstance(result.data, list):
            return result

        data = {item["ticker"]: item for item in result.data if isinstance(item, dict)}
        return FetchResult(
            success=True,
            data=data,
            provider_name=self.name,
            fetch_time=result.fetch_time,
            observation_time=result.observation_time,
            quality_status=result.quality_status,
            metadata=result.metadata,
        )


__all__ = ["SoSoValueProvider"]
