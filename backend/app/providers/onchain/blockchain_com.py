"""BlockchainComProvider — Blockchain.com 免费链上数据源。

API: https://api.blockchain.info/charts/{chart-name}
无需 API Key，覆盖网络活动与基础供应指标：
- 活跃地址 / 新增地址 / 交易笔数
- 已实现盈亏（估算） / 币天销毁 / 休眠度
- 算力 / 难度 / 市值 / 矿工收入 / 总供应量

注意：MVRV / SOPR / NUPL 等估值类指标 Blockchain.com 不提供，
统一返回 unsupported（由 Failover 切换到 Glassnode / CryptoQuant）。
"""

from datetime import datetime, timezone

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

# chart-name -> (指标名, 单位, 值缩放系数)
_CHARTS: dict[str, tuple[str, str, float]] = {
    "n-unique-addresses": ("active_addresses", "count", 1.0),
    "n-transactions": ("transaction_count", "count", 1.0),
    "n-transactions-total": ("transaction_count", "count", 1.0),
    "blocks-size": ("blocks_size", "bytes", 1.0),
    "avg-block-size": ("avg_block_size", "mb", 1.0),
    "miners-revenue": ("miner_revenue", "usd", 1.0),
    "transaction-fees": ("transaction_fees", "btc", 1.0),
    "cost-per-transaction": ("cost_per_transaction", "usd", 1.0),
    "mempool-size": ("mempool_size", "count", 1.0),
    "hash-rate": ("hash_rate", "ths", 1.0),
    "difficulty": ("difficulty", "ratio", 1.0),
    "market-cap": ("market_cap", "usd", 1.0),
    "total-bitcoins": ("total_supply", "btc", 1.0),
    "estimated-transaction-volume": ("estimated_transaction_volume", "btc", 1.0),
    "estimated-transaction-value": ("estimated_transaction_value", "usd", 1.0),
}

# 基类方法 -> chart-name 映射（仅列出本数据源支持的）
_SUPPORTED_METRICS: dict[str, str] = {
    "active_addresses": "n-unique-addresses",
    "transaction_count": "n-transactions",
    "realized_profit_loss": "estimated-transaction-value",  # 近似代理
    "hash_rate": "hash-rate",
    "difficulty": "difficulty",
    "market_cap": "market-cap",
    "total_supply": "total-bitcoins",
    "miner_revenue": "miners-revenue",
    "mempool_size": "mempool-size",
    "blocks_size": "blocks-size",
}

# 明确不支持的基类指标（返回 unsupported_result）
_UNSUPPORTED = frozenset({
    "mvrv", "realized_cap", "sopr", "asopr", "lth_sopr", "nupl",
    "puell_multiple", "rhodl", "reserve_risk", "supply_by_holder",
    "new_addresses", "hodl_waves", "coin_days_destroyed", "dormancy",
    "exchange_reserve", "netflow", "inflow", "outflow",
})


class BlockchainComProvider(BaseOnChainProvider):
    """Blockchain.com 链上数据 Provider（免费，无需 API Key）。"""

    # ---- 生命周期 ----

    async def health_check(self) -> bool:
        """轻量连通性探测：请求交易笔数图表。"""
        result = await self._request("GET", "/charts/n-transactions", params={"sampled": "false"})
        return result.success

    def get_metadata(self) -> ProviderMetadata:
        if self._metadata is None:
            self._metadata = ProviderMetadata(
                name=self.name,
                category=self.category,
                description="Blockchain.com 免费链上数据（网络活动/算力/市值/供应量）",
                supported_symbols=["BTC"],
                documentation_url="https://www.blockchain.com/charts",
            )
        return self._metadata

    def get_supported_data_types(self) -> list[str]:
        return sorted(set(_SUPPORTED_METRICS.keys()) - _UNSUPPORTED)

    # ---- 内部工具 ----

    async def _fetch_chart(
        self,
        metric_key: str,
        chart: str,
        date: datetime | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        as_history: bool = False,
    ) -> FetchResult:
        """请求 /charts/{chart} 并标准化为 FetchResult。"""
        params: dict[str, str] = {"format": "json", "sampled": "false"}
        if start:
            params["start"] = start.strftime("%Y-%m-%d")
        if end:
            params["end"] = end.strftime("%Y-%m-%d")

        result = await self._request("GET", f"/charts/{chart}", params=params)
        if not result.success:
            return result

        data = result.data
        if not isinstance(data, dict) or not data.get("values"):
            return FetchResult(
                success=False,
                error=f"Empty chart response for '{chart}'",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
                status_code=result.status_code,
                response_time_ms=result.response_time_ms,
            )

        unit = _CHARTS.get(chart, (metric_key, "ratio", 1.0))[1]
        scale = _CHARTS.get(chart, (metric_key, unit, 1.0))[2]

        # values: [[unix_ms, value], ...]（sampled=false 时为原始每日点）
        points = [
            {"t": from_unix(ts / 1000.0), "v": float(v) * scale}
            for ts, v in data["values"]
            if v is not None
        ]
        if not points:
            return FetchResult(
                success=False,
                error=f"No valid data points for '{chart}'",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
                status_code=result.status_code,
                response_time_ms=result.response_time_ms,
            )

        if as_history:
            series = [
                {
                    "value": p["v"],
                    "observation_time": p["t"].isoformat() if p["t"] else None,
                }
                for p in points
            ]
            return FetchResult(
                success=True,
                data=onchain_series_payload(
                    metric_key, series, unit=unit, resolution="1d", source=self.name
                ),
                provider_name=self.name,
                fetch_time=datetime.utcnow(),
                observation_time=points[-1]["t"],
                quality_status=QualityStatus.VERIFIED,
                raw_response=result.raw_response,
                metadata={"chart": chart, "unit": unit, "points": len(series)},
            )

        # 单点：取 date 之前（含当日）的最近一个观测值
        target = to_unix(date) if date else None
        chosen = None
        for p in points:
            if p["t"] is None:
                continue
            if target is None or to_unix(p["t"]) <= target:
                chosen = p
            else:
                break
        if chosen is None:
            return FetchResult(
                success=False,
                error=f"No observation on/before {date} for '{chart}'",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
                status_code=result.status_code,
            )

        return FetchResult(
            success=True,
            data=onchain_payload(
                metric_key,
                chosen["v"],
                chosen["t"],
                unit=unit,
                resolution="1d",
                source=self.name,
            ),
            provider_name=self.name,
            fetch_time=datetime.utcnow(),
            observation_time=chosen["t"],
            quality_status=QualityStatus.VERIFIED,
            raw_response=result.raw_response,
            status_code=result.status_code,
            response_time_ms=result.response_time_ms,
            metadata={"chart": chart, "unit": unit},
        )

    def _unsupported(self, metric: str) -> FetchResult:
        return unsupported_result(self.name, metric)

    # ---- 估值类指标（不支持，由其他 Provider 提供）----

    async def get_mvrv(self, date: datetime | None = None) -> FetchResult:
        return self._unsupported("mvrv")

    async def get_realized_cap(self, date: datetime | None = None) -> FetchResult:
        return self._unsupported("realized_cap")

    async def get_sopr(self, date: datetime | None = None) -> FetchResult:
        return self._unsupported("sopr")

    async def get_asopr(self, date: datetime | None = None) -> FetchResult:
        return self._unsupported("asopr")

    async def get_lth_sopr(self, date: datetime | None = None) -> FetchResult:
        return self._unsupported("lth_sopr")

    async def get_nupl(self, date: datetime | None = None) -> FetchResult:
        return self._unsupported("nupl")

    async def get_puell_multiple(self, date: datetime | None = None) -> FetchResult:
        return self._unsupported("puell_multiple")

    async def get_rhodl(self, date: datetime | None = None) -> FetchResult:
        return self._unsupported("rhodl")

    async def get_reserve_risk(self, date: datetime | None = None) -> FetchResult:
        return self._unsupported("reserve_risk")

    async def get_supply_by_holder(self, date: datetime | None = None) -> FetchResult:
        return self._unsupported("supply_by_holder")

    async def get_new_addresses(self, date: datetime | None = None) -> FetchResult:
        return self._unsupported("new_addresses")

    async def get_hodl_waves(self, date: datetime | None = None) -> FetchResult:
        return self._unsupported("hodl_waves")

    async def get_coin_days_destroyed(self, date: datetime | None = None) -> FetchResult:
        return self._unsupported("coin_days_destroyed")

    async def get_dormancy(self, date: datetime | None = None) -> FetchResult:
        return self._unsupported("dormancy")

    # ---- 交易所资金流（不支持）----

    async def get_exchange_reserve(self, date: datetime | None = None) -> FetchResult:
        return self._unsupported("exchange_reserve")

    async def get_netflow(self, date: datetime | None = None) -> FetchResult:
        return self._unsupported("netflow")

    async def get_inflow(self, date: datetime | None = None) -> FetchResult:
        return self._unsupported("inflow")

    async def get_outflow(self, date: datetime | None = None) -> FetchResult:
        return self._unsupported("outflow")

    # ---- 支持的指标 ----

    async def get_active_addresses(self, date: datetime | None = None) -> FetchResult:
        """活跃地址数（/charts/n-unique-addresses）。"""
        return await self._fetch_chart("active_addresses", "n-unique-addresses", date=date)

    async def get_transaction_count(self, date: datetime | None = None) -> FetchResult:
        """链上交易笔数（/charts/n-transactions）。"""
        return await self._fetch_chart("transaction_count", "n-transactions", date=date)

    async def get_realized_profit_loss(self, date: datetime | None = None) -> FetchResult:
        """估算链上转账价值（作为 realized profit/loss 的近似代理，标记 ESTIMATED）。"""
        result = await self._fetch_chart(
            "realized_profit_loss", "estimated-transaction-value", date=date
        )
        if result.success:
            result.quality_status = QualityStatus.ESTIMATED
            result.metadata["note"] = "estimated-transaction-value 为近似代理，非真实已实现盈亏"
        return result

    async def get_hash_rate(self, date: datetime | None = None) -> FetchResult:
        """全网算力（TH/s）。"""
        return await self._fetch_chart("hash_rate", "hash-rate", date=date)

    async def get_difficulty(self, date: datetime | None = None) -> FetchResult:
        """挖矿难度。"""
        return await self._fetch_chart("difficulty", "difficulty", date=date)

    async def get_market_cap(self, date: datetime | None = None) -> FetchResult:
        """市值（USD）。"""
        return await self._fetch_chart("market_cap", "market-cap", date=date)

    async def get_total_supply(self, date: datetime | None = None) -> FetchResult:
        """BTC 总供应量。"""
        return await self._fetch_chart("total_supply", "total-bitcoins", date=date)

    async def get_miner_revenue(self, date: datetime | None = None) -> FetchResult:
        """矿工每日收入（USD）。"""
        return await self._fetch_chart("miner_revenue", "miners-revenue", date=date)

    async def get_metric_history(
        self, metric: str, start: datetime, end: datetime
    ) -> FetchResult:
        """批量获取指标历史序列。"""
        chart = _SUPPORTED_METRICS.get(metric)
        if not chart or metric in _UNSUPPORTED:
            return self._unsupported(metric)

        start_utc = start.astimezone(timezone.utc).replace(tzinfo=None) if start.tzinfo else start
        end_utc = end.astimezone(timezone.utc).replace(tzinfo=None) if end.tzinfo else end

        result = await self._fetch_chart(
            metric, chart, start=start_utc, end=end_utc, as_history=True
        )
        if result.success:
            logger.debug(
                f"[{self.name}] metric history '{metric}': "
                f"{result.data.get('count', 0)} points"
            )
        return result


__all__ = ["BlockchainComProvider"]
