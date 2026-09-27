"""WorldBankProvider — 世界银行公开数据宏观数据源。

API: https://api.worldbank.org/v2/country/{country}/indicator/{code}
（完全免费，无需 API Key；年度数据为主，更新滞后但权威）

覆盖指标（World Bank indicator code）：
- GDP（现价美元）: NY.GDP.MKTP.CD
- GDP 年增长率: NY.GDP.MKTP.KD.ZG
- 通胀率（CPI YoY %）: FP.CPI.TOTL.ZG
- 失业率: SL.UEM.TOTL.ZS
- M2（广义货币，本币）: FM.LBL.BMNY.CN

利率/汇率类指标（DXY、Fed Rate、国债收益率等）World Bank 不提供，
标记为 UNSUPPORTED，由 Failover 切换到 FRED。

Look-ahead Bias 说明：World Bank API 不提供发布时间字段，
release_date 置空并在 metadata 标注 release_date_unknown=true；
年度数据的 observation_date 为该年 12 月 31 日，回测时应保守使用次年数据。
"""

from datetime import datetime
from typing import Any

from app.providers.base.macro_provider import BaseMacroProvider
from app.providers.base.payloads import macro_payload, unsupported_result
from app.providers.base.types import (
    ErrorType,
    FetchResult,
    ProviderMetadata,
    QualityStatus,
)
from app.utils.datetime_utils import utcnow

# 指标 key -> (indicator code, 单位, 频率说明)
_INDICATORS: dict[str, tuple[str, str, str]] = {
    "gdp": ("NY.GDP.MKTP.CD", "usd", "yearly"),
    "gdp_growth": ("NY.GDP.MKTP.KD.ZG", "percent_yoy", "yearly"),
    "inflation": ("FP.CPI.TOTL.ZG", "percent_yoy", "yearly"),
    "cpi": ("FP.CPI.TOTL.ZG", "percent_yoy", "yearly"),  # CPI YoY 口径
    "unemployment": ("SL.UEM.TOTL.ZS", "percent", "yearly"),
    "m2": ("FM.LBL.BMNY.CN", "local_currency", "yearly"),
}


def _parse_year_date(value: Any) -> datetime | None:
    """解析 World Bank 的 date 字段（通常为年份，如 "2023"）。

    年度数据的观测期结束于当年 12 月 31 日。
    """
    if value is None:
        return None
    try:
        year = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if year < 1800 or year > 2200:
        return None
    return datetime(year, 12, 31)


class WorldBankProvider(BaseMacroProvider):
    """世界银行宏观数据 Provider（免费，无需 API Key）。"""

    # 注册名（与 providers.yaml 配置 key 对齐）
    provider_key = "world_bank"

    # 默认查询国家（World Bank 支持任意 ISO2 国家码）
    DEFAULT_COUNTRY = "US"

    # ---- 生命周期 ----

    async def health_check(self) -> bool:
        """连通性探测：请求美国 GDP 最近 1 年数据。"""
        result = await self._fetch_indicator_raw("NY.GDP.MKTP.CD", limit=5)
        return result.success

    def get_metadata(self) -> ProviderMetadata:
        if self._metadata is None:
            self._metadata = ProviderMetadata(
                name=self.name,
                category=self.category,
                description="世界银行宏观数据（GDP/通胀/失业率/M2，年度权威口径）",
                supported_symbols=["USD"],
                data_coverage_start=datetime(1960, 1, 1),
                documentation_url="https://datahelpdesk.worldbank.org/knowledgebase/articles/889392",
            )
        return self._metadata

    def get_supported_data_types(self) -> list[str]:
        return sorted(set(_INDICATORS.keys()))

    # ---- 内部工具 ----

    async def _fetch_indicator_raw(
        self,
        indicator_code: str,
        *,
        country: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
    ) -> FetchResult:
        """请求 /country/{code}/indicator/{indicator} 并校验响应结构。

        World Bank 响应格式：[元信息 dict, 观测列表] 或 [元信息, None]（无数据）。
        """
        country = (country or self.DEFAULT_COUNTRY).upper()
        path = f"/country/{country}/indicator/{indicator_code}"
        params: dict[str, Any] = {
            "format": "json",
            "per_page": limit,
        }
        if start and end:
            params["date"] = f"{start.year}:{end.year}"
        elif not end:
            params["mrv"] = 1  # 无时间约束时仅取最新修订版本（Most Recent Value）

        result = await self._request("GET", path, params=params)
        if not result.success:
            return result

        body = result.data
        # 正常响应为 [meta, observations] 两元素数组
        observations: Any = None
        if isinstance(body, list) and len(body) >= 2:
            observations = body[1]
        elif isinstance(body, dict) and isinstance(body.get("1"), list):
            observations = body["1"]

        if not isinstance(observations, list) or not observations:
            return FetchResult(
                success=False,
                error=f"No World Bank data for '{indicator_code}' ({country})",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
                status_code=result.status_code,
            )

        result.data = observations
        return result

    @staticmethod
    def _clean_observations(observations: list[Any]) -> list[dict[str, Any]]:
        """过滤空值并按观测日期升序排列。"""
        points: list[dict[str, Any]] = []
        for obs in observations:
            if not isinstance(obs, dict):
                continue
            raw_value = obs.get("value")
            if raw_value is None:
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            obs_date = _parse_year_date(obs.get("date"))
            if obs_date is None:
                continue
            points.append({"observation_date": obs_date, "value": value})
        points.sort(key=lambda p: p["observation_date"])
        return points

    async def _fetch_indicator(
        self, indicator: str, date: datetime | None = None
    ) -> FetchResult:
        """获取单指标在 date（或最新）时点的 MacroDataPoint 载荷。"""
        spec = _INDICATORS.get(indicator)
        if not spec:
            return unsupported_result(self.name, indicator)
        indicator_code, unit, frequency = spec

        result = await self._fetch_indicator_raw(
            indicator_code, end=date, limit=100
        )
        if not result.success:
            return result

        points = self._clean_observations(result.data)
        if not points:
            return FetchResult(
                success=False,
                error=f"No valid observations for '{indicator_code}'",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
                status_code=result.status_code,
            )

        # 取 <= date 的最后一个观测（date 为 None 时取最新）
        chosen = points[-1]
        if date is not None:
            eligible = [p for p in points if p["observation_date"] <= date]
            if not eligible:
                return FetchResult(
                    success=False,
                    error=f"No '{indicator}' observation before {date:%Y-%m-%d}",
                    error_type=ErrorType.EMPTY_RESPONSE,
                    provider_name=self.name,
                )
            chosen = eligible[-1]

        return FetchResult(
            success=True,
            data=macro_payload(
                indicator,
                chosen["value"],
                chosen["observation_date"],
                release_date=None,  # World Bank 不提供发布时间
                unit=unit,
                frequency=frequency,
                source=self.name,
                extra={
                    "indicator_code": indicator_code,
                    "release_date_unknown": True,
                    "country": self.DEFAULT_COUNTRY,
                },
            ),
            provider_name=self.name,
            fetch_time=utcnow(),
            observation_time=chosen["observation_date"],
            # 缺少 release_date，回测使用需谨慎，降级为 ESTIMATED
            quality_status=QualityStatus.ESTIMATED,
            raw_response=result.raw_response,
            status_code=result.status_code,
            response_time_ms=result.response_time_ms,
            metadata={"indicator_code": indicator_code, "frequency": frequency},
        )

    # ---- 基类接口实现 ----

    async def get_dxy(self, date: datetime | None = None) -> FetchResult:
        """美元指数：World Bank 不提供（由 FRED 覆盖）。"""
        return unsupported_result(self.name, "dxy")

    async def get_fed_rate(self, date: datetime | None = None) -> FetchResult:
        """联邦基金利率：World Bank 不提供（由 FRED 覆盖）。"""
        return unsupported_result(self.name, "fed_rate")

    async def get_treasury_yield(
        self, maturity: str = "10y", date: datetime | None = None
    ) -> FetchResult:
        """国债收益率：World Bank 不提供（由 FRED 覆盖）。"""
        return unsupported_result(self.name, f"treasury_{maturity}")

    async def get_real_yield(self, date: datetime | None = None) -> FetchResult:
        """实际收益率：World Bank 不提供（由 FRED 覆盖）。"""
        return unsupported_result(self.name, "real_yield")

    async def get_cpi(self, date: datetime | None = None) -> FetchResult:
        """CPI 通胀率（FP.CPI.TOTL.ZG，年度 YoY %）。"""
        return await self._fetch_indicator("cpi", date)

    async def get_pce(self, date: datetime | None = None) -> FetchResult:
        """PCE：World Bank 不提供（由 FRED 覆盖）。"""
        return unsupported_result(self.name, "pce")

    async def get_unemployment(self, date: datetime | None = None) -> FetchResult:
        """失业率（SL.UEM.TOTL.ZS，年度 %，ILO 口径）。"""
        return await self._fetch_indicator("unemployment", date)

    async def get_nfp(self, date: datetime | None = None) -> FetchResult:
        """非农就业：World Bank 不提供（由 FRED 覆盖）。"""
        return unsupported_result(self.name, "nfp")

    async def get_gdp(self, date: datetime | None = None) -> FetchResult:
        """GDP（NY.GDP.MKTP.CD，现价美元，年度）。"""
        return await self._fetch_indicator("gdp", date)

    async def get_m2(self, date: datetime | None = None) -> FetchResult:
        """M2 广义货币供应量（FM.LBL.BMNY.CN，本币，年度）。"""
        return await self._fetch_indicator("m2", date)

    async def get_fed_balance_sheet(self, date: datetime | None = None) -> FetchResult:
        """美联储资产负债表：World Bank 不提供（由 FRED 覆盖）。"""
        return unsupported_result(self.name, "fed_balance_sheet")

    async def get_global_liquidity(self, date: datetime | None = None) -> FetchResult:
        """全球流动性指数：World Bank 无直接序列。"""
        return unsupported_result(self.name, "global_liquidity")

    # ---- 扩展方法（非基类接口）----

    async def get_inflation(self, date: datetime | None = None) -> FetchResult:
        """通胀率（FP.CPI.TOTL.ZG，年度 YoY %）。"""
        return await self._fetch_indicator("inflation", date)

    async def get_gdp_growth(self, date: datetime | None = None) -> FetchResult:
        """GDP 年增长率（NY.GDP.MKTP.KD.ZG，不变价 YoY %）。"""
        return await self._fetch_indicator("gdp_growth", date)

    # ---- 批量历史 ----

    async def get_macro_series(
        self, indicator: str, start: datetime, end: datetime
    ) -> FetchResult:
        """批量获取宏观指标年度历史序列。"""
        spec = _INDICATORS.get(indicator)
        if not spec:
            return unsupported_result(self.name, indicator)
        indicator_code, unit, frequency = spec

        result = await self._fetch_indicator_raw(
            indicator_code, start=start, end=end, limit=200
        )
        if not result.success:
            return result

        points = self._clean_observations(result.data)
        points = [
            p for p in points
            if start.year <= p["observation_date"].year <= end.year
        ]
        if not points:
            return FetchResult(
                success=False,
                error=f"No '{indicator}' observations in [{start:%Y}, {end:%Y}]",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
                status_code=result.status_code,
            )

        series = [
            macro_payload(
                indicator,
                p["value"],
                p["observation_date"],
                release_date=None,
                unit=unit,
                frequency=frequency,
                source=self.name,
                extra={"indicator_code": indicator_code, "release_date_unknown": True},
            )
            for p in points
        ]
        return FetchResult(
            success=True,
            data=series,
            provider_name=self.name,
            fetch_time=utcnow(),
            observation_time=points[-1]["observation_date"],
            quality_status=QualityStatus.ESTIMATED,
            raw_response=result.raw_response,
            status_code=result.status_code,
            response_time_ms=result.response_time_ms,
            metadata={
                "indicator_code": indicator_code,
                "count": len(series),
                "unit": unit,
            },
        )


__all__ = ["WorldBankProvider"]
