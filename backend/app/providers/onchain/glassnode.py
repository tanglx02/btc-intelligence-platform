"""GlassnodeProvider — Glassnode 链上指标数据源。

API: https://api.glassnode.com/v1/metrics/{endpoint}
需要 API Key（免费层级 Tier 1 仅提供部分指标，高级指标返回 401/403，
统一映射为 AUTH_ERROR，由 Failover 引擎切换到其他 Provider）。

覆盖指标：
- 估值：MVRV / SOPR / aSOPR / LTH-SOPR / NUPL / Puell Multiple / Reserve Risk / RHODL
- 市值：Realized Cap / Market Cap
- 持有者：LTH Supply / STH Supply / HODL Waves / Dormancy / CDD
- 网络：Active Addresses / New Addresses / Transaction Count
- 交易所流：Netflow / Inflow / Outflow / Exchange Reserve
"""

from datetime import datetime, timezone
from typing import Any

from loguru import logger

from app.providers.base.onchain_provider import BaseOnChainProvider
from app.providers.base.payloads import (
    from_unix,
    onchain_payload,
    onchain_series_payload,
    to_unix,
    unsupported_result,
)
from app.providers.base.types import (
    ErrorType,
    FetchResult,
    ProviderMetadata,
    QualityStatus,
)

# 指标 key -> (API endpoint, 单位)
# endpoint 相对于 base_url（https://api.glassnode.com/v1）
_ENDPOINTS: dict[str, tuple[str, str]] = {
    # 估值类
    "mvrv": ("metrics/market/mvrv", "ratio"),
    "sopr": ("metrics/indicators/sopr", "ratio"),
    "asopr": ("metrics/indicators/adjusted_sopr", "ratio"),
    "lth_sopr": ("metrics/indicators/sopr_lth", "ratio"),
    "nupl": ("metrics/market/nupl", "ratio"),
    "puell_multiple": ("metrics/indicators/puell_multiple", "ratio"),
    "reserve_risk": ("metrics/indicators/reserve_risk", "ratio"),
    "rhodl": ("metrics/indicators/rhodl_ratio", "ratio"),
    "realized_cap": ("metrics/market/realized_cap", "usd"),
    "market_cap": ("metrics/market/market_cap_usd", "usd"),
    # 持有者行为
    "lth_supply": ("metrics/supply/balance_lth", "btc"),
    "sth_supply": ("metrics/supply/balance_sth", "btc"),
    "hodl_waves_1d": ("metrics/supply/dormancy_1d", "btc"),
    "dormancy": ("metrics/indicators/dormancy", "ratio"),
    "coin_days_destroyed": ("metrics/indicators/coin_days_destroyed", "days"),
    "realized_profit": ("metrics/profit/realized_profit", "usd"),
    "realized_loss": ("metrics/profit/realized_loss", "usd"),
    # 网络活动
    "active_addresses": ("metrics/addresses/active_count", "count"),
    "new_addresses": ("metrics/addresses/new_count", "count"),
    "transaction_count": ("metrics/transactions/count", "count"),
    # 交易所资金流
    "netflow": ("metrics/transactions/transfers_volume_exchanges_net", "btc"),
    "inflow": ("metrics/transactions/transfers_volume_in_exchanges_native", "btc"),
    "outflow": ("metrics/transactions/transfers_volume_out_exchanges_native", "btc"),
    "exchange_reserve": ("metrics/balance/total_balance_exchanges_native", "btc"),
}

# 免费层级（Tier 1）通常可用的指标；其余指标大概率返回 401/403
_FREE_TIER_METRICS = frozenset({
    "active_addresses", "transaction_count", "market_cap",
    "sopr", "nupl", "lth_supply", "sth_supply",
})

_DEFAULT_START = "2010-01-03"  # BTC 创世后首个可用日期


class GlassnodeProvider(BaseOnChainProvider):
    """Glassnode 链上指标 Provider。"""

    # ---- 生命周期 ----

    async def health_check(self) -> bool:
        """连通性探测：请求免费层级的活跃地址指标。"""
        result = await self._fetch_raw(
            "metrics/addresses/active_count", limit=1, resolution="24h"
        )
        return result.success

    def get_metadata(self) -> ProviderMetadata:
        if self._metadata is None:
            self._metadata = ProviderMetadata(
                name=self.name,
                category=self.category,
                description="Glassnode 链上指标（MVRV/SOPR/NUPL 等，免费层级受限）",
                supported_symbols=["BTC"],
                supported_intervals=["10m", "1h", "24h", "1w", "1month"],
                data_coverage_start=datetime(2010, 1, 3),
                documentation_url="https://docs.glassnode.com/",
            )
        return self._metadata

    def get_supported_data_types(self) -> list[str]:
        return sorted(_ENDPOINTS.keys())

    # ---- 内部工具 ----

    def _resolution(self) -> str:
        """默认数据粒度（可通过 config.extra.resolution 覆盖）。"""
        return str(self._config.extra.get("resolution", "24h"))

    async def _fetch_raw(
        self,
        endpoint: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
        resolution: str | None = None,
    ) -> FetchResult:
        """调用 Glassnode metrics API。

        公共参数：a=btc, i=resolution, s=start, t=end, f=json, c=UTC
        API Key 通过 api_key 参数传递（Glassnode 不支持 Bearer Header）。
        """
        if not self._config.api_key:
            return FetchResult(
                success=False,
                error="GLASSNODE_API_KEY not configured",
                error_type=ErrorType.AUTH_ERROR,
                provider_name=self.name,
            )

        params: dict[str, Any] = {
            "a": "btc",
            "i": resolution or self._resolution(),
            "f": "json",
            "c": "UTC",
            "api_key": self._config.api_key,
        }
        if start:
            params["s"] = to_unix(start)
        else:
            params["s"] = _DEFAULT_START
        if end:
            params["t"] = to_unix(end)
        if limit:
            params["limit"] = limit

        return await self._request("GET", f"/{endpoint}", params=params)

    def _normalize_series(self, raw: Any) -> list[dict[str, Any]]:
        """将 Glassnode 响应 [{t, v}] 标准化（t 为 Unix 秒，v 可能为标量或 dict）。"""
        points: list[dict[str, Any]] = []
        if not isinstance(raw, list):
            return points
        for item in raw:
            if not isinstance(item, dict):
                continue
            ts = item.get("t")
            v = item.get("v")
            # 部分指标返回 dict（如 HODL Waves 各时间段分量）
            if isinstance(v, dict):
                for sub_key, sub_val in v.items():
                    if sub_val is not None:
                        points.append({
                            "t": from_unix(ts),
                            "v": float(sub_val),
                            "cohort": str(sub_key),
                        })
            elif v is not None:
                try:
                    points.append({"t": from_unix(ts), "v": float(v), "cohort": None})
                except (TypeError, ValueError):
                    continue
        return points

    async def _fetch_metric(
        self,
        metric_key: str,
        date: datetime | None = None,
        cohort_filter: str | None = None,
    ) -> FetchResult:
        """获取单个指标在 date（或最新）时点的值。"""
        spec = _ENDPOINTS.get(metric_key)
        if not spec:
            return unsupported_result(self.name, metric_key)
        endpoint, unit = spec

        # date 语义：取 <= date 的最近一个观测点
        result = await self._fetch_raw(endpoint, end=date, limit=1000 if not date else None)
        if not result.success:
            if result.error_type in (ErrorType.AUTH_ERROR,) or result.status_code in (401, 403):
                result.error_type = ErrorType.AUTH_ERROR
                result.error = (
                    f"Glassnode metric '{metric_key}' unavailable on current plan: "
                    f"{result.error}"
                )
                if metric_key not in _FREE_TIER_METRICS:
                    logger.debug(
                        f"[{self.name}] metric '{metric_key}' likely requires paid tier"
                    )
            return result

        points = self._normalize_series(result.data)
        if cohort_filter:
            points = [p for p in points if p.get("cohort") == cohort_filter]
        if not points:
            return FetchResult(
                success=False,
                error=f"No data points for metric '{metric_key}'",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
                status_code=result.status_code,
                response_time_ms=result.response_time_ms,
            )

        # 已按时间升序返回，取最后一个 <= date 的点
        chosen = points[-1]
        if date:
            candidates = [p for p in points if p["t"] and to_unix(p["t"]) <= to_unix(date)]
            if not candidates:
                return FetchResult(
                    success=False,
                    error=f"No observation on/before {date} for '{metric_key}'",
                    error_type=ErrorType.EMPTY_RESPONSE,
                    provider_name=self.name,
                )
            chosen = candidates[-1]

        return FetchResult(
            success=True,
            data=onchain_payload(
                metric_key,
                chosen["v"],
                chosen["t"],
                unit=unit,
                resolution=self._resolution(),
                source=self.name,
                extra={"cohort": chosen.get("cohort")} if chosen.get("cohort") else None,
            ),
            provider_name=self.name,
            fetch_time=datetime.utcnow(),
            observation_time=chosen["t"],
            quality_status=QualityStatus.VERIFIED,
            raw_response=result.raw_response,
            status_code=result.status_code,
            response_time_ms=result.response_time_ms,
            metadata={"endpoint": endpoint, "unit": unit, "free_tier": metric_key in _FREE_TIER_METRICS},
        )

    # ---- 估值类指标 ----

    async def get_mvrv(self, date: datetime | None = None) -> FetchResult:
        """MVRV（/market/mvrv，免费层级可能受限）。"""
        return await self._fetch_metric("mvrv", date)

    async def get_realized_cap(self, date: datetime | None = None) -> FetchResult:
        """已实现市值（/market/realized_cap）。"""
        return await self._fetch_metric("realized_cap", date)

    async def get_sopr(self, date: datetime | None = None) -> FetchResult:
        """SOPR（/indicators/sopr）。"""
        return await self._fetch_metric("sopr", date)

    async def get_asopr(self, date: datetime | None = None) -> FetchResult:
        """调整后 SOPR（/indicators/adjusted_sopr）。"""
        return await self._fetch_metric("asopr", date)

    async def get_lth_sopr(self, date: datetime | None = None) -> FetchResult:
        """长期持有者 SOPR（/indicators/sopr_lth）。"""
        return await self._fetch_metric("lth_sopr", date)

    async def get_nupl(self, date: datetime | None = None) -> FetchResult:
        """NUPL（/market/nupl）。"""
        return await self._fetch_metric("nupl", date)

    async def get_puell_multiple(self, date: datetime | None = None) -> FetchResult:
        """Puell Multiple（/indicators/puell_multiple）。"""
        return await self._fetch_metric("puell_multiple", date)

    async def get_rhodl(self, date: datetime | None = None) -> FetchResult:
        """RHODL Ratio（/indicators/rhodl_ratio）。"""
        return await self._fetch_metric("rhodl", date)

    async def get_reserve_risk(self, date: datetime | None = None) -> FetchResult:
        """Reserve Risk（/indicators/reserve_risk）。"""
        return await self._fetch_metric("reserve_risk", date)

    async def get_realized_profit_loss(self, date: datetime | None = None) -> FetchResult:
        """已实现盈亏（profit 与 loss 两个端点合并返回）。"""
        profit = await self._fetch_metric("realized_profit", date)
        loss = await self._fetch_metric("realized_loss", date)
        if not profit.success and not loss.success:
            return profit if not profit.success else loss

        data: dict[str, Any] = {
            "metric_name": "realized_profit_loss",
            "source": self.name,
        }
        if profit.success and isinstance(profit.data, dict):
            data["realized_profit_usd"] = profit.data.get("value")
            data["observation_time"] = profit.data.get("observation_time")
        if loss.success and isinstance(loss.data, dict):
            data["realized_loss_usd"] = loss.data.get("value")
        return FetchResult(
            success=True,
            data=data,
            provider_name=self.name,
            fetch_time=datetime.utcnow(),
            observation_time=profit.observation_time or loss.observation_time,
            quality_status=QualityStatus.VERIFIED,
            metadata={"unit": "usd"},
        )

    # ---- 持有者行为 ----

    async def get_supply_by_holder(self, date: datetime | None = None) -> FetchResult:
        """LTH/STH Supply 分布。"""
        lth = await self._fetch_metric("lth_supply", date)
        sth = await self._fetch_metric("sth_supply", date)
        if not lth.success and not sth.success:
            return lth

        data: dict[str, Any] = {
            "metric_name": "supply_by_holder",
            "source": self.name,
        }
        if lth.success and isinstance(lth.data, dict):
            data["lth_supply_btc"] = lth.data.get("value")
            data["observation_time"] = lth.data.get("observation_time")
        if sth.success and isinstance(sth.data, dict):
            data["sth_supply_btc"] = sth.data.get("value")
        return FetchResult(
            success=True,
            data=data,
            provider_name=self.name,
            fetch_time=datetime.utcnow(),
            observation_time=lth.observation_time or sth.observation_time,
            quality_status=QualityStatus.VERIFIED,
            metadata={"unit": "btc"},
        )

    async def get_lth_supply(self, date: datetime | None = None) -> FetchResult:
        """长期持有者供应量（/supply/balance_lth）。"""
        return await self._fetch_metric("lth_supply", date)

    async def get_sth_supply(self, date: datetime | None = None) -> FetchResult:
        """短期持有者供应量（/supply/balance_sth）。"""
        return await self._fetch_metric("sth_supply", date)

    async def get_hodl_waves(self, date: datetime | None = None) -> FetchResult:
        """HODL Waves 持币时间分布（返回多 cohort 载荷）。"""
        return await self._fetch_metric("hodl_waves_1d", date)

    async def get_coin_days_destroyed(self, date: datetime | None = None) -> FetchResult:
        """币天销毁（/indicators/coin_days_destroyed）。"""
        return await self._fetch_metric("coin_days_destroyed", date)

    async def get_dormancy(self, date: datetime | None = None) -> FetchResult:
        """休眠度（/indicators/dormancy）。"""
        return await self._fetch_metric("dormancy", date)

    # ---- 网络活动 ----

    async def get_active_addresses(self, date: datetime | None = None) -> FetchResult:
        """活跃地址数（/addresses/active_count）。"""
        return await self._fetch_metric("active_addresses", date)

    async def get_new_addresses(self, date: datetime | None = None) -> FetchResult:
        """新增地址数（/addresses/new_count）。"""
        return await self._fetch_metric("new_addresses", date)

    async def get_transaction_count(self, date: datetime | None = None) -> FetchResult:
        """链上交易笔数（/transactions/count）。"""
        return await self._fetch_metric("transaction_count", date)

    # ---- 交易所资金流 ----

    async def get_exchange_reserve(self, date: datetime | None = None) -> FetchResult:
        """交易所 BTC 储备量。"""
        return await self._fetch_metric("exchange_reserve", date)

    async def get_netflow(self, date: datetime | None = None) -> FetchResult:
        """交易所净流入/流出（正=净流入）。"""
        result = await self._fetch_metric("netflow", date)
        if result.success and isinstance(result.data, dict):
            result.data["flow_type"] = "netflow"
        return result

    async def get_inflow(self, date: datetime | None = None) -> FetchResult:
        """交易所流入量。"""
        result = await self._fetch_metric("inflow", date)
        if result.success and isinstance(result.data, dict):
            result.data["flow_type"] = "inflow"
        return result

    async def get_outflow(self, date: datetime | None = None) -> FetchResult:
        """交易所流出量。"""
        result = await self._fetch_metric("outflow", date)
        if result.success and isinstance(result.data, dict):
            result.data["flow_type"] = "outflow"
        return result

    # ---- 批量历史 ----

    async def get_metric_history(
        self, metric: str, start: datetime, end: datetime
    ) -> FetchResult:
        """批量获取某链上指标的历史序列。"""
        spec = _ENDPOINTS.get(metric)
        if not spec:
            return unsupported_result(self.name, metric)
        endpoint, unit = spec

        start_utc = start.astimezone(timezone.utc).replace(tzinfo=None) if start.tzinfo else start
        end_utc = end.astimezone(timezone.utc).replace(tzinfo=None) if end.tzinfo else end

        result = await self._fetch_raw(endpoint, start=start_utc, end=end_utc)
        if not result.success:
            return result

        points = self._normalize_series(result.data)
        if not points:
            return FetchResult(
                success=False,
                error=f"Empty history for metric '{metric}'",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
                status_code=result.status_code,
            )

        series = [
            {
                "value": p["v"],
                "observation_time": p["t"].isoformat() if p["t"] else None,
                **({"cohort": p["cohort"]} if p.get("cohort") else {}),
            }
            for p in points
        ]
        return FetchResult(
            success=True,
            data=onchain_series_payload(
                metric, series, unit=unit, resolution=self._resolution(), source=self.name
            ),
            provider_name=self.name,
            fetch_time=datetime.utcnow(),
            observation_time=points[-1]["t"],
            quality_status=QualityStatus.VERIFIED,
            raw_response=result.raw_response,
            status_code=result.status_code,
            response_time_ms=result.response_time_ms,
            metadata={"endpoint": endpoint, "unit": unit},
        )


__all__ = ["GlassnodeProvider"]
