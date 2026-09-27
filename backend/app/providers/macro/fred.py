"""FREDProvider — 美联储经济数据库（FRED）宏观数据源。

API: https://api.stlouisfed.org/fred/series/observations（免费，需 API Key，
Key 通过 api_key 查询参数传递，可在 https://fredaccount.stlouisfed.org/ 免费申请）

覆盖指标（series_id）：
- DXY 美元指数: DTWEXBGS（贸易加权广义美元指数）
- 联邦基金利率: FEDFUNDS / DFEDTARU（目标上限）
- 国债收益率: DGS2 / DGS5 / DGS10 / DGS30
- 实际收益率: DFII10（10Y TIPS）
- CPI: CPIAUCSL（含 YoY 计算）/ PCE: PCEPI（含 YoY 计算）
- 失业率: UNRATE / 非农就业: PAYEMS
- GDP: GDPC1 / M2: M2SL / 美联储资产负债表: WALCL

Look-ahead Bias 防护：载荷严格区分 observation_date（数据所属期）、
release_date（首次可得日期，取 FRED realtime_start）、revision_date。
"""

from datetime import datetime, timedelta
from typing import Any

from app.providers.base.macro_provider import BaseMacroProvider
from app.providers.base.payloads import (
    auth_required_result,
    macro_payload,
    unsupported_result,
)
from app.providers.base.types import (
    ErrorType,
    FetchResult,
    ProviderMetadata,
    QualityStatus,
)
from app.utils.datetime_utils import utcnow

# 指标 key -> (series_id, 单位, 频率说明, 是否计算 YoY)
_SERIES: dict[str, tuple[str, str, str, bool]] = {
    "dxy": ("DTWEXBGS", "index", "daily", False),
    "fed_rate": ("FEDFUNDS", "percent", "monthly", False),
    "fed_rate_target_upper": ("DFEDTARU", "percent", "meeting", False),
    "treasury_2y": ("DGS2", "percent", "daily", False),
    "treasury_5y": ("DGS5", "percent", "daily", False),
    "treasury_10y": ("DGS10", "percent", "daily", False),
    "treasury_30y": ("DGS30", "percent", "daily", False),
    "real_yield": ("DFII10", "percent", "daily", False),
    "cpi": ("CPIAUCSL", "index", "monthly", True),
    "pce": ("PCEPI", "index", "monthly", True),
    "unemployment": ("UNRATE", "percent", "monthly", False),
    "nfp": ("PAYEMS", "thousands", "monthly", False),
    "gdp": ("GDPC1", "billions_usd", "quarterly", False),
    "m2": ("M2SL", "billions_usd", "monthly", False),
    "fed_balance_sheet": ("WALCL", "millions_usd", "weekly", False),
}

# 期限 -> 指标 key
_MATURITY_MAP = {
    "2y": "treasury_2y",
    "5y": "treasury_5y",
    "10y": "treasury_10y",
    "30y": "treasury_30y",
    "3m": None,  # FRED 有 DGS3MO，此处未纳入
}


def _parse_date(value: str | None) -> datetime | None:
    """解析 FRED 日期（YYYY-MM-DD）。"""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


class FREDProvider(BaseMacroProvider):
    """FRED 宏观经济数据 Provider。"""

    # 注册名（与 providers.yaml 配置 key 对齐；snake_case 推导会产生 f_r_e_d）
    provider_key = "fred"

    # ---- 生命周期 ----

    async def health_check(self) -> bool:
        """连通性探测：请求 10Y 国债收益率最近 1 个观测值。"""
        result = await self._fetch_observations("DGS10", sort_desc=True, limit=1)
        return result.success

    def get_metadata(self) -> ProviderMetadata:
        if self._metadata is None:
            self._metadata = ProviderMetadata(
                name=self.name,
                category=self.category,
                description="FRED 宏观经济数据（利率/美元指数/通胀/就业/流动性）",
                supported_symbols=["USD"],
                data_coverage_start=datetime(1913, 1, 1),
                documentation_url="https://fred.stlouisfed.org/docs/api/fred/",
            )
        return self._metadata

    def get_supported_data_types(self) -> list[str]:
        return sorted(_SERIES.keys())

    # ---- 内部工具 ----

    async def _fetch_observations(
        self,
        series_id: str,
        *,
        observation_start: datetime | None = None,
        observation_end: datetime | None = None,
        sort_desc: bool = False,
        limit: int | None = None,
    ) -> FetchResult:
        """请求 /series/observations 并做基础校验。"""
        if not self._config.api_key:
            return auth_required_result(
                self.name, "FRED_API_KEY not configured (free at fredaccount.stlouisfed.org)"
            )

        params: dict[str, Any] = {
            "series_id": series_id,
            "api_key": self._config.api_key,
            "file_type": "json",
            "sort_order": "desc" if sort_desc else "asc",
        }
        if observation_start:
            params["observation_start"] = observation_start.strftime("%Y-%m-%d")
        if observation_end:
            params["observation_end"] = observation_end.strftime("%Y-%m-%d")
        if limit:
            params["limit"] = limit

        result = await self._request("GET", "/series/observations", params=params)
        if not result.success:
            if result.status_code in (400, 401, 403):
                result.error_type = ErrorType.AUTH_ERROR
                result.error = f"FRED api_key invalid or unauthorized: {result.error}"
            return result

        body = result.data
        if not isinstance(body, dict) or "observations" not in body:
            return FetchResult(
                success=False,
                error=f"Unexpected FRED response for '{series_id}'",
                error_type=ErrorType.DATA_FORMAT,
                provider_name=self.name,
                status_code=result.status_code,
            )
        return result

    @staticmethod
    def _clean_observations(body: dict[str, Any]) -> list[dict[str, Any]]:
        """过滤 '.'（缺失值）并标准化观测点。"""
        points: list[dict[str, Any]] = []
        for obs in body.get("observations", []) or []:
            if not isinstance(obs, dict):
                continue
            raw_value = obs.get("value")
            if raw_value in (".", "", None):
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            points.append({
                "observation_date": _parse_date(obs.get("date")),
                "value": value,
                # realtime_start 即该观测值首次可得的日期（防未来数据泄漏）
                "release_date": _parse_date(obs.get("realtime_start")),
                "revision_date": _parse_date(obs.get("realtime_end"))
                if obs.get("realtime_end") != obs.get("realtime_start")
                else None,
            })
        return points

    async def _fetch_indicator(
        self, indicator: str, date: datetime | None = None, yoy: bool = False
    ) -> FetchResult:
        """获取单指标在 date（或最新）时点的 MacroDataPoint 载荷。"""
        spec = _SERIES.get(indicator)
        if not spec:
            return unsupported_result(self.name, indicator)
        series_id, unit, frequency, wants_yoy = spec
        yoy = yoy or wants_yoy

        end = date
        # YoY 需要至少 13 个月度观测；普通指标取最近若干条选最后一条 <= date
        if yoy:
            start = (date or utcnow()) - timedelta(days=500)
            result = await self._fetch_observations(
                series_id, observation_start=start, observation_end=end
            )
        else:
            result = await self._fetch_observations(
                series_id, observation_end=end, sort_desc=True, limit=30
            )
        if not result.success:
            return result

        points = self._clean_observations(result.data)
        if not points:
            return FetchResult(
                success=False,
                error=f"No observations for series '{series_id}'",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
                status_code=result.status_code,
            )

        # 统一按 observation_date 升序
        points.sort(key=lambda p: p["observation_date"] or datetime.min)
        chosen = points[-1]

        extra: dict[str, Any] = {"series_id": series_id}
        value = chosen["value"]
        if yoy and len(points) >= 13:
            base_point = points[-13]
            if base_point["value"]:
                extra["yoy_percent"] = (value / base_point["value"] - 1.0) * 100.0
                extra["yoy_base_date"] = (
                    base_point["observation_date"].isoformat()
                    if base_point["observation_date"] else None
                )

        return FetchResult(
            success=True,
            data=macro_payload(
                indicator,
                value,
                chosen["observation_date"] or utcnow(),
                release_date=chosen["release_date"],
                revision_date=chosen["revision_date"],
                unit=unit,
                frequency=frequency,
                source=self.name,
                extra=extra,
            ),
            provider_name=self.name,
            fetch_time=utcnow(),
            observation_time=chosen["observation_date"],
            quality_status=QualityStatus.VERIFIED,
            raw_response=result.raw_response,
            status_code=result.status_code,
            response_time_ms=result.response_time_ms,
            metadata={"series_id": series_id, "frequency": frequency},
        )

    # ---- 基类接口实现 ----

    async def get_dxy(self, date: datetime | None = None) -> FetchResult:
        """美元指数（贸易加权广义指数 DTWEXBGS）。"""
        return await self._fetch_indicator("dxy", date)

    async def get_fed_rate(self, date: datetime | None = None) -> FetchResult:
        """联邦基金有效利率（FEDFUNDS，月度均值）。"""
        return await self._fetch_indicator("fed_rate", date)

    async def get_treasury_yield(
        self, maturity: str = "10y", date: datetime | None = None
    ) -> FetchResult:
        """国债收益率（2y/5y/10y/30y）。"""
        indicator = _MATURITY_MAP.get(maturity.lower())
        if not indicator:
            return FetchResult(
                success=False,
                error=f"Unsupported maturity '{maturity}' (use 2y/5y/10y/30y)",
                error_type=ErrorType.DATA_FORMAT,
                provider_name=self.name,
            )
        return await self._fetch_indicator(indicator, date)

    async def get_real_yield(self, date: datetime | None = None) -> FetchResult:
        """10Y 实际收益率（TIPS，DFII10）。"""
        return await self._fetch_indicator("real_yield", date)

    async def get_cpi(self, date: datetime | None = None) -> FetchResult:
        """CPI 指数（附 YoY 同比，metadata.yoy_percent）。"""
        return await self._fetch_indicator("cpi", date)

    async def get_pce(self, date: datetime | None = None) -> FetchResult:
        """PCE 指数（附 YoY 同比，metadata.yoy_percent）。"""
        return await self._fetch_indicator("pce", date)

    async def get_unemployment(self, date: datetime | None = None) -> FetchResult:
        """失业率（UNRATE）。"""
        return await self._fetch_indicator("unemployment", date)

    async def get_nfp(self, date: datetime | None = None) -> FetchResult:
        """非农就业总人数（PAYEMS，千人）。"""
        return await self._fetch_indicator("nfp", date)

    async def get_gdp(self, date: datetime | None = None) -> FetchResult:
        """实际 GDP（GDPC1，十亿美元）。"""
        return await self._fetch_indicator("gdp", date)

    async def get_m2(self, date: datetime | None = None) -> FetchResult:
        """M2 货币供应量（M2SL，十亿美元）。"""
        return await self._fetch_indicator("m2", date)

    async def get_fed_balance_sheet(self, date: datetime | None = None) -> FetchResult:
        """美联储资产负债表总资产（WALCL，百万美元，周度）。"""
        return await self._fetch_indicator("fed_balance_sheet", date)

    async def get_global_liquidity(self, date: datetime | None = None) -> FetchResult:
        """全球流动性指数：FRED 无直接序列（可由 M2 + WALCL 在引擎层合成）。"""
        return unsupported_result(self.name, "global_liquidity")

    # ---- 批量历史 ----

    async def get_macro_series(
        self, indicator: str, start: datetime, end: datetime
    ) -> FetchResult:
        """批量获取宏观指标历史序列（含 release_date，防 Look-ahead Bias）。"""
        spec = _SERIES.get(indicator)
        if not spec:
            return unsupported_result(self.name, indicator)
        series_id, unit, frequency, _ = spec

        result = await self._fetch_observations(
            series_id, observation_start=start, observation_end=end
        )
        if not result.success:
            return result

        points = self._clean_observations(result.data)
        if not points:
            return FetchResult(
                success=False,
                error=f"No observations for '{series_id}' in [{start}, {end}]",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
                status_code=result.status_code,
            )

        series = [
            macro_payload(
                indicator,
                p["value"],
                p["observation_date"] or utcnow(),
                release_date=p["release_date"],
                revision_date=p["revision_date"],
                unit=unit,
                frequency=frequency,
                source=self.name,
            )
            for p in points
        ]
        return FetchResult(
            success=True,
            data=series,
            provider_name=self.name,
            fetch_time=utcnow(),
            observation_time=points[-1]["observation_date"],
            quality_status=QualityStatus.VERIFIED,
            raw_response=result.raw_response,
            status_code=result.status_code,
            response_time_ms=result.response_time_ms,
            metadata={"series_id": series_id, "count": len(series), "unit": unit},
        )


__all__ = ["FREDProvider"]
