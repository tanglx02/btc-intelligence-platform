"""CryptoQuantProvider — CryptoQuant 链上指标与交易所资金流数据源。

API: https://api.cryptoquant.com/v1/btc/{group}/{metric}
需要 API Key（通过 X-API-Key 请求头传递；免费层级数据延迟且指标受限，
高级指标返回 401/403，统一映射为 AUTH_ERROR 由 Failover 处理）。

覆盖指标：
- 估值：MVRV / SOPR / Realized Cap / Puell Multiple / Reserve Risk / NUPL
- 交易所流：Reserve / Inflow / Outflow / Netflow（支持按交易所细分）
- 矿工：Miner Flow / Miner Reserve / Miner Position Index
- 鲸鱼：Whale Alert / 大额转账比率
"""

from datetime import datetime, timezone
from typing import Any

from app.providers.base.onchain_provider import BaseOnChainProvider
from app.providers.base.payloads import (
    exchange_flow_payload,
    onchain_payload,
    onchain_series_payload,
    unsupported_result,
)
from app.providers.base.types import (
    ErrorType,
    FetchResult,
    ProviderMetadata,
    QualityStatus,
)

# 指标 key -> (endpoint group/path, 单位, 数据形态)
# 数据形态: "metric" 标量指标 / "flow" 资金流
_ENDPOINTS: dict[str, tuple[str, str, str]] = {
    # 估值类（market-data）
    "mvrv": ("btc/market-data/mvrv", "ratio", "metric"),
    "sopr": ("btc/market-data/sopr", "ratio", "metric"),
    "realized_cap": ("btc/market-data/realized-cap", "usd", "metric"),
    "market_cap": ("btc/market-data/market-cap", "usd", "metric"),
    "puell_multiple": ("btc/market-data/puell-multiple", "ratio", "metric"),
    "reserve_risk": ("btc/market-data/reserve-risk", "ratio", "metric"),
    "nupl": ("btc/market-data/nupl", "ratio", "metric"),
    # 交易所资金流（exchange-flows）
    "exchange_reserve": ("btc/exchange-flows/reserve", "btc", "flow"),
    "inflow": ("btc/exchange-flows/inflow", "btc", "flow"),
    "outflow": ("btc/exchange-flows/outflow", "btc", "flow"),
    "netflow": ("btc/exchange-flows/netflow", "btc", "flow"),
    # 矿工（miner-flows）
    "miner_flow": ("btc/miner-flows/miner-flow", "btc", "flow"),
    "miner_reserve": ("btc/miner-flows/miner-reserve", "btc", "flow"),
    "mpi": ("btc/miner-flows/miner-position-index", "ratio", "metric"),
    # 鲸鱼 / 大额转账
    "whale_ratio": ("btc/exchange-flows/whale-ratio", "ratio", "metric"),
    "whale_inflow": ("btc/exchange-flows/whale-inflow", "btc", "flow"),
    # 网络活动
    "active_addresses": ("btc/network-data/active-addresses", "count", "metric"),
    "transaction_count": ("btc/network-data/transactions-count", "count", "metric"),
}


def _parse_iso_date(value: str | None) -> datetime | None:
    """解析 CryptoQuant 的 ISO8601 日期字符串。"""
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        return None


class CryptoQuantProvider(BaseOnChainProvider):
    """CryptoQuant 链上指标 Provider。"""

    # 注册名（与 providers.yaml 配置 key 对齐；snake_case 推导会产生 crypto_quant）
    provider_key = "cryptoquant"
    # 同一类在 exchange_flow 类别下的别名注册（YAML: cryptoquant_flow）
    aliases = ("cryptoquant_flow",)

    # ---- 生命周期 ----

    async def health_check(self) -> bool:
        """连通性探测：请求交易所储备（1 天窗口）。"""
        result = await self._fetch_raw("btc/exchange-flows/reserve", window="day", limit=1)
        return result.success

    def get_metadata(self) -> ProviderMetadata:
        if self._metadata is None:
            self._metadata = ProviderMetadata(
                name=self.name,
                category=self.category,
                description="CryptoQuant 链上指标与交易所/矿工/鲸鱼资金流",
                supported_symbols=["BTC"],
                supported_intervals=["15m", "hour", "day"],
                data_coverage_start=datetime(2010, 1, 3),
                documentation_url="https://docs.cryptoquant.com/",
            )
        return self._metadata

    def get_supported_data_types(self) -> list[str]:
        return sorted(_ENDPOINTS.keys())

    # ---- 内部工具 ----

    def _auth_headers(self) -> dict[str, str]:
        """CryptoQuant 使用 X-API-Key 请求头鉴权。"""
        return {"X-API-Key": self._config.api_key or ""}

    async def _fetch_raw(
        self,
        path: str,
        *,
        window: str = "day",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
        exchange: str = "all",
    ) -> FetchResult:
        """调用 CryptoQuant API（自动附加鉴权头）。"""
        if not self._config.api_key:
            return FetchResult(
                success=False,
                error="CRYPTOQUANT_API_KEY not configured",
                error_type=ErrorType.AUTH_ERROR,
                provider_name=self.name,
            )

        params: dict[str, Any] = {"window": window, "format": "json"}
        if start:
            params["s"] = start.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        if end:
            params["e"] = end.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        if limit:
            params["limit"] = limit
        if exchange and exchange != "all":
            params["exchange"] = exchange

        result = await self._request(
            "GET", f"/{path}", params=params, headers=self._auth_headers()
        )
        if not result.success and result.status_code in (401, 403):
            result.error_type = ErrorType.AUTH_ERROR
            result.error = f"CryptoQuant plan/auth restriction: {result.error}"
        return result

    @staticmethod
    def _normalize_rows(raw: Any) -> list[dict[str, Any]]:
        """标准化 CryptoQuant 响应 {data: [{d: iso_date, v: ...}]}。"""
        rows: list[dict[str, Any]] = []
        if not isinstance(raw, dict):
            return rows
        for item in raw.get("data", []) or []:
            if not isinstance(item, dict):
                continue
            rows.append({
                "t": _parse_iso_date(item.get("d")),
                "v": item.get("v"),
            })
        return rows

    async def _fetch_metric(
        self, metric_key: str, date: datetime | None = None
    ) -> FetchResult:
        """获取标量类指标在 date（或最新）时点的值。"""
        spec = _ENDPOINTS.get(metric_key)
        if not spec or spec[2] != "metric":
            return unsupported_result(self.name, metric_key)
        path, unit, _ = spec

        result = await self._fetch_raw(path, window="day", end=date)
        if not result.success:
            return result

        rows = self._normalize_rows(result.data)
        if not rows or rows[-1]["v"] is None:
            return FetchResult(
                success=False,
                error=f"No data points for metric '{metric_key}'",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
                status_code=result.status_code,
            )

        chosen = rows[-1]
        try:
            value = float(chosen["v"])
        except (TypeError, ValueError):
            return FetchResult(
                success=False,
                error=f"Invalid value for '{metric_key}': {chosen['v']!r}",
                error_type=ErrorType.DATA_FORMAT,
                provider_name=self.name,
            )

        return FetchResult(
            success=True,
            data=onchain_payload(
                metric_key, value, chosen["t"], unit=unit, resolution="1d", source=self.name
            ),
            provider_name=self.name,
            fetch_time=datetime.utcnow(),
            observation_time=chosen["t"],
            quality_status=QualityStatus.VERIFIED,
            raw_response=result.raw_response,
            status_code=result.status_code,
            response_time_ms=result.response_time_ms,
            metadata={"endpoint": path, "unit": unit},
        )

    async def _fetch_flow(
        self,
        metric_key: str,
        date: datetime | None = None,
        exchange: str = "all",
    ) -> FetchResult:
        """获取资金流类指标（ExchangeFlowData 载荷）。"""
        spec = _ENDPOINTS.get(metric_key)
        if not spec or spec[2] != "flow":
            return unsupported_result(self.name, metric_key)
        path, unit, _ = spec

        result = await self._fetch_raw(path, window="day", end=date, exchange=exchange)
        if not result.success:
            return result

        rows = self._normalize_rows(result.data)
        if not rows or rows[-1]["v"] is None:
            return FetchResult(
                success=False,
                error=f"No flow data for '{metric_key}'",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
                status_code=result.status_code,
            )

        chosen = rows[-1]
        try:
            amount_btc = float(chosen["v"])
        except (TypeError, ValueError):
            return FetchResult(
                success=False,
                error=f"Invalid flow value for '{metric_key}': {chosen['v']!r}",
                error_type=ErrorType.DATA_FORMAT,
                provider_name=self.name,
            )

        flow_type = {
            "exchange_reserve": "reserve",
            "inflow": "inflow",
            "outflow": "outflow",
            "netflow": "netflow",
            "miner_flow": "miner_flow",
            "miner_reserve": "miner_reserve",
            "whale_inflow": "whale_inflow",
        }.get(metric_key, metric_key)

        return FetchResult(
            success=True,
            data=exchange_flow_payload(
                flow_type,
                amount_btc=amount_btc,
                exchange=exchange if exchange != "all" else None,
                observation_time=chosen["t"],
                source=self.name,
                extra={"unit": unit, "metric_key": metric_key},
            ),
            provider_name=self.name,
            fetch_time=datetime.utcnow(),
            observation_time=chosen["t"],
            quality_status=QualityStatus.VERIFIED,
            raw_response=result.raw_response,
            status_code=result.status_code,
            response_time_ms=result.response_time_ms,
            metadata={"endpoint": path, "unit": unit, "flow_type": flow_type},
        )

    # ---- 估值类指标 ----

    async def get_mvrv(self, date: datetime | None = None) -> FetchResult:
        """MVRV。"""
        return await self._fetch_metric("mvrv", date)

    async def get_realized_cap(self, date: datetime | None = None) -> FetchResult:
        """已实现市值。"""
        return await self._fetch_metric("realized_cap", date)

    async def get_sopr(self, date: datetime | None = None) -> FetchResult:
        """SOPR。"""
        return await self._fetch_metric("sopr", date)

    async def get_asopr(self, date: datetime | None = None) -> FetchResult:
        """CryptoQuant 不单独提供 aSOPR。"""
        return unsupported_result(self.name, "asopr")

    async def get_lth_sopr(self, date: datetime | None = None) -> FetchResult:
        """CryptoQuant 不单独提供 LTH-SOPR。"""
        return unsupported_result(self.name, "lth_sopr")

    async def get_nupl(self, date: datetime | None = None) -> FetchResult:
        """NUPL。"""
        return await self._fetch_metric("nupl", date)

    async def get_puell_multiple(self, date: datetime | None = None) -> FetchResult:
        """Puell Multiple。"""
        return await self._fetch_metric("puell_multiple", date)

    async def get_rhodl(self, date: datetime | None = None) -> FetchResult:
        """CryptoQuant 不提供 RHODL。"""
        return unsupported_result(self.name, "rhodl")

    async def get_reserve_risk(self, date: datetime | None = None) -> FetchResult:
        """Reserve Risk。"""
        return await self._fetch_metric("reserve_risk", date)

    async def get_realized_profit_loss(self, date: datetime | None = None) -> FetchResult:
        """CryptoQuant 免费版无直接的已实现盈亏端点。"""
        return unsupported_result(self.name, "realized_profit_loss")

    # ---- 持有者行为 ----

    async def get_supply_by_holder(self, date: datetime | None = None) -> FetchResult:
        """CryptoQuant 无 LTH/STH supply 端点。"""
        return unsupported_result(self.name, "supply_by_holder")

    async def get_hodl_waves(self, date: datetime | None = None) -> FetchResult:
        """CryptoQuant 无 HODL Waves 端点。"""
        return unsupported_result(self.name, "hodl_waves")

    async def get_coin_days_destroyed(self, date: datetime | None = None) -> FetchResult:
        """CryptoQuant 无 CDD 端点。"""
        return unsupported_result(self.name, "coin_days_destroyed")

    async def get_dormancy(self, date: datetime | None = None) -> FetchResult:
        """CryptoQuant 无 Dormancy 端点。"""
        return unsupported_result(self.name, "dormancy")

    # ---- 网络活动 ----

    async def get_active_addresses(self, date: datetime | None = None) -> FetchResult:
        """活跃地址数。"""
        return await self._fetch_metric("active_addresses", date)

    async def get_new_addresses(self, date: datetime | None = None) -> FetchResult:
        """CryptoQuant 无新增地址端点。"""
        return unsupported_result(self.name, "new_addresses")

    async def get_transaction_count(self, date: datetime | None = None) -> FetchResult:
        """链上交易笔数。"""
        return await self._fetch_metric("transaction_count", date)

    # ---- 交易所资金流 ----

    async def get_exchange_reserve(self, date: datetime | None = None) -> FetchResult:
        """交易所 BTC 储备量（exchange=all 为全网汇总）。"""
        return await self._fetch_flow("exchange_reserve", date)

    async def get_netflow(self, date: datetime | None = None) -> FetchResult:
        """交易所净流入/流出。"""
        return await self._fetch_flow("netflow", date)

    async def get_inflow(self, date: datetime | None = None) -> FetchResult:
        """交易所流入量。"""
        return await self._fetch_flow("inflow", date)

    async def get_outflow(self, date: datetime | None = None) -> FetchResult:
        """交易所流出量。"""
        return await self._fetch_flow("outflow", date)

    # ---- CryptoQuant 特有指标 ----

    async def get_miner_flow(self, date: datetime | None = None) -> FetchResult:
        """矿工流出量（流向交易所的 BTC）。"""
        return await self._fetch_flow("miner_flow", date)

    async def get_miner_reserve(self, date: datetime | None = None) -> FetchResult:
        """矿工钱包储备。"""
        return await self._fetch_flow("miner_reserve", date)

    async def get_miner_position_index(self, date: datetime | None = None) -> FetchResult:
        """矿工持仓指数 (MPI)。"""
        return await self._fetch_metric("mpi", date)

    async def get_whale_activity(self, date: datetime | None = None) -> FetchResult:
        """鲸鱼活动（交易所鲸鱼比率 + 鲸鱼流入量合并载荷）。"""
        ratio = await self._fetch_metric("whale_ratio", date)
        inflow = await self._fetch_flow("whale_inflow", date)
        if not ratio.success and not inflow.success:
            return ratio

        data: dict[str, Any] = {"metric_name": "whale_activity", "source": self.name}
        if ratio.success and isinstance(ratio.data, dict):
            data["whale_ratio"] = ratio.data.get("value")
            data["observation_time"] = ratio.data.get("observation_time")
        if inflow.success and isinstance(inflow.data, dict):
            data["whale_inflow_btc"] = inflow.data.get("amount_btc")
        return FetchResult(
            success=True,
            data=data,
            provider_name=self.name,
            fetch_time=datetime.utcnow(),
            observation_time=ratio.observation_time or inflow.observation_time,
            quality_status=QualityStatus.VERIFIED,
        )

    # ---- 批量历史 ----

    async def get_metric_history(
        self, metric: str, start: datetime, end: datetime
    ) -> FetchResult:
        """批量获取某链上指标的历史序列。"""
        spec = _ENDPOINTS.get(metric)
        if not spec:
            return unsupported_result(self.name, metric)
        path, unit, kind = spec

        start_utc = start.astimezone(timezone.utc).replace(tzinfo=None) if start.tzinfo else start
        end_utc = end.astimezone(timezone.utc).replace(tzinfo=None) if end.tzinfo else end

        result = await self._fetch_raw(path, window="day", start=start_utc, end=end_utc)
        if not result.success:
            return result

        rows = self._normalize_rows(result.data)
        if not rows:
            return FetchResult(
                success=False,
                error=f"Empty history for metric '{metric}'",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
                status_code=result.status_code,
            )

        series = []
        for row in rows:
            if row["v"] is None:
                continue
            try:
                series.append({
                    "value": float(row["v"]),
                    "observation_time": row["t"].isoformat() if row["t"] else None,
                })
            except (TypeError, ValueError):
                continue

        return FetchResult(
            success=True,
            data=onchain_series_payload(
                metric, series, unit=unit, resolution="1d", source=self.name
            ),
            provider_name=self.name,
            fetch_time=datetime.utcnow(),
            quality_status=QualityStatus.VERIFIED,
            raw_response=result.raw_response,
            status_code=result.status_code,
            response_time_ms=result.response_time_ms,
            metadata={"endpoint": path, "unit": unit, "kind": kind},
        )


__all__ = ["CryptoQuantProvider"]
