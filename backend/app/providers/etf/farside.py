"""FarsideProvider — Farside Investors 现货 ETF 资金流数据源（网页抓取）。

URL: https://farside.co.uk/btc/
免费，无需 API Key。页面为每日各 BTC 现货 ETF 的净流入/流出表格（百万美元）：
- 负值以括号表示，如 (93.7)
- 空单元格 / "F" 表示 0
- 行首列为日期（dd Mmm yyyy），"Total" 行为合计列

实现方式：httpx 拉取 HTML -> BeautifulSoup(lxml) 解析表格 -> 标准化载荷。
解析结果内存缓存 30 分钟，降低抓取频率。
"""

import time
from datetime import datetime, timedelta
from typing import Any

from loguru import logger

from app.providers.base.etf_provider import BaseETFProvider
from app.providers.base.payloads import etf_flow_payload, unsupported_result
from app.providers.base.types import (
    ErrorType,
    FetchResult,
    ProviderMetadata,
    QualityStatus,
)
from app.utils.datetime_utils import as_naive_utc, utcnow

# 页面路径（相对 base_url https://farside.co.uk）
_BTC_PAGE = "/btc/"
# 解析结果缓存 TTL（秒）— Farside 每日更新一次，30 分钟足够新鲜
_CACHE_TTL = 1800.0
# 合计列名称（不作为基金代码）
_TOTAL_COLUMN = "Total"
# 周期 -> 天数
_PERIOD_DAYS = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}


def _parse_flow_number(text: str) -> float | None:
    """解析资金流单元格文本（单位：百万美元 -> 转换为美元）。

    "(93.7)" -> -93_700_000.0；"201.4" -> 201_400_000.0；
    "" / "F" / "n.a." -> 0.0；无法解析 -> None
    """
    cleaned = text.replace(",", "").replace("\u00a0", "").strip()
    if cleaned in ("", "F", "f"):
        return 0.0
    if cleaned in ("n.a.", "N/A", "-", "?"):
        return None

    negative = cleaned.startswith("(") and cleaned.endswith(")")
    if negative:
        cleaned = cleaned[1:-1]
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return -value * 1_000_000 if negative else value * 1_000_000


def _parse_row_date(text: str) -> datetime | None:
    """解析日期单元格：仅接受 'dd Mmm yyyy'（跳过 'Mmm yyyy' 月度行与 'Total' 行）。"""
    cleaned = " ".join(text.split())
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


class FarsideProvider(BaseETFProvider):
    """Farside Investors 现货 ETF 资金流 Provider（网页抓取）。"""

    def __init__(self, config: Any):
        super().__init__(config)
        # 解析结果缓存：{"rows": [...], "cached_at": monotonic_seconds}
        self._table_cache: dict[str, Any] | None = None

    # ---- 生命周期 ----

    async def health_check(self) -> bool:
        """连通性探测：抓取页面并确认可解析出数据行。"""
        result = await self._request("GET", _BTC_PAGE, headers={"Accept": "text/html"})
        if not result.success or not isinstance(result.data, str):
            return False
        rows = self._parse_table(result.data)
        return len(rows) > 0

    def get_metadata(self) -> ProviderMetadata:
        if self._metadata is None:
            self._metadata = ProviderMetadata(
                name=self.name,
                category=self.category,
                description="Farside 美国现货 BTC ETF 每日资金流（网页抓取，单位 USD）",
                supported_symbols=["BTC"],
                data_coverage_start=datetime(2024, 1, 11),  # 美国现货 ETF 上市日
                documentation_url="https://farside.co.uk/btc/",
            )
        return self._metadata

    def get_supported_data_types(self) -> list[str]:
        return ["daily_flow", "net_flow", "cumulative_flow", "all_funds_flow"]

    # ---- 抓取与解析 ----

    async def _get_rows(self) -> tuple[list[dict[str, Any]], str | None]:
        """获取解析后的表格行（带内存缓存）。

        Returns:
            (rows, error)：成功时 error 为 None
            rows 结构：[{"date": datetime, "flows": {ticker: usd}, "total": usd}]
        """
        now = time.monotonic()
        if self._table_cache and now - self._table_cache["cached_at"] < _CACHE_TTL:
            return self._table_cache["rows"], None

        result = await self._request("GET", _BTC_PAGE, headers={"Accept": "text/html"})
        if not result.success:
            return [], result.error or "fetch failed"
        if not isinstance(result.data, str):
            return [], "unexpected non-HTML response from farside.co.uk"

        rows = self._parse_table(result.data)
        if not rows:
            return [], "failed to parse ETF flow table (page structure may have changed)"

        self._table_cache = {"rows": rows, "cached_at": now}
        logger.debug(f"[{self.name}] parsed {len(rows)} ETF flow rows")
        return rows, None

    def _parse_table(self, html: str) -> list[dict[str, Any]]:
        """解析 Farside BTC ETF 页面表格。"""
        try:
            from bs4 import BeautifulSoup
        except ImportError:  # pragma: no cover - 依赖缺失时优雅降级
            logger.error(f"[{self.name}] beautifulsoup4 not installed, cannot parse HTML")
            return []

        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:  # noqa: BLE001 - lxml 缺失时回退内置解析器
            soup = BeautifulSoup(html, "html.parser")

        table = soup.find("table")
        if table is None:
            return []

        # 表头 -> 基金代码列表（跳过说明性文字列）
        header_cells: list[str] = []
        for tr in table.find_all("tr"):
            cells = [th.get_text(" ", strip=True) for th in tr.find_all(["th", "td"])]
            if any(c.upper() == _TOTAL_COLUMN.upper() for c in cells):
                header_cells = cells
                break
        if not header_cells:
            # 兜底：使用第一行作为表头
            first_tr = table.find("tr")
            if first_tr is None:
                return []
            header_cells = [c.get_text(" ", strip=True) for c in first_tr.find_all(["th", "td"])]

        tickers: list[str | None] = []
        for cell in header_cells:
            normalized = cell.strip().upper()
            if normalized == _TOTAL_COLUMN.upper():
                tickers.append(_TOTAL_COLUMN)
            elif normalized in ("", "DATE"):
                tickers.append(None)
            elif normalized.replace(".", "").replace("-", "").isalnum() and len(normalized) <= 6:
                tickers.append(normalized)
            else:
                tickers.append(None)

        rows: list[dict[str, Any]] = []
        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if not cells:
                continue
            date = _parse_row_date(cells[0].get_text(" ", strip=True))
            if date is None:
                continue  # 跳过月度行 / 合计行 / 表头

            flows: dict[str, float] = {}
            total: float | None = None
            for idx, cell in enumerate(cells[1:], start=1):
                if idx >= len(tickers):
                    break
                ticker = tickers[idx]
                if ticker is None:
                    continue
                value = _parse_flow_number(cell.get_text(" ", strip=True))
                if value is None:
                    continue
                if ticker == _TOTAL_COLUMN:
                    total = value
                else:
                    flows[ticker] = value

            if total is None and flows:
                total = sum(flows.values())
            rows.append({"date": date, "flows": flows, "total": total or 0.0})

        rows.sort(key=lambda r: r["date"])
        return rows

    @staticmethod
    def _find_row(rows: list[dict[str, Any]], date: datetime | None) -> dict[str, Any] | None:
        """按日期查找行：精确匹配 -> 之前最近一行 -> None。"""
        if not rows:
            return None
        if date is None:
            return rows[-1]
        # date 可能来自 Service/引擎（aware UTC）；row["date"] 为 naive UTC，归一化后比较
        target = as_naive_utc(date).replace(hour=0, minute=0, second=0, microsecond=0)
        matched = None
        for row in rows:
            if row["date"] <= target:
                matched = row
            else:
                break
        return matched

    # ---- 基类接口实现 ----

    async def get_daily_flow(self, date: datetime | None = None) -> FetchResult:
        """获取指定日期 ETF 每日净流入/流出（含 TOTAL 与各基金明细）。"""
        rows, error = await self._get_rows()
        if error:
            return FetchResult(
                success=False,
                error=error,
                error_type=ErrorType.DATA_FORMAT if "parse" in error else ErrorType.NETWORK_ERROR,
                provider_name=self.name,
            )

        row = self._find_row(rows, date)
        if row is None:
            return FetchResult(
                success=False,
                error=f"No ETF flow data on/before {date}",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
            )

        data = [
            etf_flow_payload(
                "TOTAL",
                "net",
                row["total"],
                date=row["date"],
                fund_name="All US Spot BTC ETFs",
                source=self.name,
            )
        ]
        for ticker, amount in sorted(row["flows"].items()):
            data.append(
                etf_flow_payload(
                    ticker,
                    "net",
                    amount,
                    date=row["date"],
                    source=self.name,
                )
            )

        return FetchResult(
            success=True,
            data=data,
            provider_name=self.name,
            fetch_time=utcnow(),
            observation_time=row["date"],
            quality_status=QualityStatus.VERIFIED,
            metadata={"unit": "usd", "funds": len(row["flows"])},
        )

    async def get_net_flow(
        self, period: str = "7d", end_date: datetime | None = None
    ) -> FetchResult:
        """获取指定周期净流量（对 TOTAL 列求和）。"""
        days = _PERIOD_DAYS.get(period)
        if days is None:
            return FetchResult(
                success=False,
                error=f"Unsupported period '{period}' (use 1d/7d/30d/90d)",
                error_type=ErrorType.DATA_FORMAT,
                provider_name=self.name,
            )

        rows, error = await self._get_rows()
        if error:
            return FetchResult(
                success=False,
                error=error,
                error_type=ErrorType.NETWORK_ERROR,
                provider_name=self.name,
            )

        end_row = self._find_row(rows, end_date)
        if end_row is None:
            return FetchResult(
                success=False,
                error=f"No ETF flow data on/before {end_date}",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
            )

        start_boundary = end_row["date"] - timedelta(days=days)
        window = [r for r in rows if start_boundary < r["date"] <= end_row["date"]]
        net_total = sum(r["total"] for r in window)

        data = etf_flow_payload(
            "TOTAL",
            "net",
            net_total,
            date=end_row["date"],
            period=period,
            fund_name="All US Spot BTC ETFs",
            source=self.name,
        )
        return FetchResult(
            success=True,
            data=data,
            provider_name=self.name,
            fetch_time=utcnow(),
            observation_time=end_row["date"],
            quality_status=QualityStatus.VERIFIED,
            metadata={"unit": "usd", "days_covered": len(window), "period": period},
        )

    async def get_cumulative_flow(self, start: datetime, end: datetime) -> FetchResult:
        """获取累计流量序列（按日 running sum，自上市日以来累计基线）。"""
        rows, error = await self._get_rows()
        if error:
            return FetchResult(
                success=False,
                error=error,
                error_type=ErrorType.NETWORK_ERROR,
                provider_name=self.name,
            )

        # start/end 可能来自 Service/引擎（aware UTC）；row["date"] 为 naive UTC，归一化后比较
        start_day = as_naive_utc(start).replace(hour=0, minute=0, second=0, microsecond=0)
        end_day = as_naive_utc(end).replace(hour=0, minute=0, second=0, microsecond=0)

        # 累计基线：start 之前所有日的净流入之和
        baseline = sum(r["total"] for r in rows if r["date"] < start_day)
        cumulative = baseline
        data: list[dict[str, Any]] = []
        for row in rows:
            if row["date"] < start_day:
                continue
            if row["date"] > end_day:
                break
            cumulative += row["total"]
            data.append(
                etf_flow_payload(
                    "TOTAL",
                    "cumulative",
                    row["total"],
                    date=row["date"],
                    cumulative_usd=cumulative,
                    fund_name="All US Spot BTC ETFs",
                    source=self.name,
                )
            )

        if not data:
            return FetchResult(
                success=False,
                error=f"No ETF flow rows in [{start_day}, {end_day}]",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
            )

        return FetchResult(
            success=True,
            data=data,
            provider_name=self.name,
            fetch_time=utcnow(),
            observation_time=rows[-1]["date"],
            quality_status=QualityStatus.VERIFIED,
            metadata={"unit": "usd", "baseline_usd": baseline, "points": len(data)},
        )

    async def get_holdings(self, date: datetime | None = None) -> FetchResult:
        """Farside 流量页不提供持仓量数据。"""
        return unsupported_result(self.name, "holdings")

    async def get_all_funds_flow(self, date: datetime | None = None) -> FetchResult:
        """获取所有 ETF 基金的分基金流量明细（dict[ticker, payload]）。"""
        rows, error = await self._get_rows()
        if error:
            return FetchResult(
                success=False,
                error=error,
                error_type=ErrorType.NETWORK_ERROR,
                provider_name=self.name,
            )

        row = self._find_row(rows, date)
        if row is None:
            return FetchResult(
                success=False,
                error=f"No ETF flow data on/before {date}",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
            )

        data = {
            ticker: etf_flow_payload(
                ticker,
                "net",
                amount,
                date=row["date"],
                source=self.name,
            )
            for ticker, amount in row["flows"].items()
        }
        return FetchResult(
            success=True,
            data=data,
            provider_name=self.name,
            fetch_time=utcnow(),
            observation_time=row["date"],
            quality_status=QualityStatus.VERIFIED,
            metadata={"unit": "usd", "total_usd": row["total"]},
        )


__all__ = ["FarsideProvider"]
