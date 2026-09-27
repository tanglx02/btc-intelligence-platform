"""DeribitProvider — Deribit 期权数据源（全球最大 BTC 期权交易所）。

API: https://www.deribit.com/api/v2/public/...（公共端点，无需 API Key）
覆盖：
- 期权未平仓量 / 成交量（按 Call/Put 分解）: public/get_book_summary_by_currency
- 隐含波动率: public/get_book_summary_by_currency + DVOL 指数（public/get_volatility_index_data）
- Put/Call Ratio: 由 book summary 聚合计算
- 波动率偏斜: public/get_order_book_by_currency（25-Delta 风险逆转，线性插值）
- Gamma 敞口: 由 greeks + OI 估算（假设做市商 delta 对冲，长 Call 短 Put 惯例）
- 到期日数据: public/get_instruments
"""

import re
import time
from datetime import datetime, timedelta
from typing import Any

from app.providers.base.options_provider import BaseOptionsProvider
from app.providers.base.payloads import options_payload
from app.providers.base.types import (
    ErrorType,
    FetchResult,
    ProviderMetadata,
    QualityStatus,
)
from app.utils.datetime_utils import utcnow

# 微型缓存 TTL（book summary 被多个方法复用）
_CACHE_TTL = 60.0

# 期权合约名格式：BTC-27SEP24-60000-C
_INSTRUMENT_RE = re.compile(
    r"^(?P<base>[A-Z]+)-(?P<expiry>\d{1,2}[A-Z]{3}\d{2})-(?P<strike>[\d.]+)-(?P<kind>[CP])$"
)

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_instrument(name: str) -> dict[str, Any] | None:
    """解析 Deribit 期权合约名 -> {expiry, strike, kind(C/P)}。"""
    match = _INSTRUMENT_RE.match(name)
    if not match:
        return None
    expiry_raw = match.group("expiry")
    try:
        day = int(expiry_raw[: len(expiry_raw) - 5])
        month = _MONTHS[expiry_raw[-5:-2]]
        year = 2000 + int(expiry_raw[-2:])
        expiry = datetime(year, month, day, 8, 0)  # Deribit 到期结算 08:00 UTC
    except (ValueError, KeyError):
        return None
    return {
        "expiry": expiry,
        "strike": float(match.group("strike")),
        "kind": "call" if match.group("kind") == "C" else "put",
    }


def _interpolate_iv_at_delta(
    points: list[tuple[float, float]], target_delta: float
) -> float | None:
    """按 |delta| 对 IV 做线性插值。

    points: [(abs_delta, iv), ...]，至少两个点才能插值；
    单点时直接返回该点 IV（近似）。
    """
    if not points:
        return None
    points = sorted(points, key=lambda p: p[0])
    if len(points) == 1:
        return points[0][1]
    if target_delta <= points[0][0]:
        return points[0][1]
    if target_delta >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        if x0 <= target_delta <= x1:
            if x1 == x0:
                return y0
            ratio = (target_delta - x0) / (x1 - x0)
            return y0 + ratio * (y1 - y0)
    return None


class DeribitProvider(BaseOptionsProvider):
    """Deribit 期权数据 Provider（免费公共 API）。"""

    def __init__(self, config: Any):
        super().__init__(config)
        self._cache: dict[str, Any] = {}

    # ---- 生命周期 ----

    async def health_check(self) -> bool:
        """连通性探测：public/test。"""
        result = await self._get("/public/test")
        return result.success

    def get_metadata(self) -> ProviderMetadata:
        if self._metadata is None:
            self._metadata = ProviderMetadata(
                name=self.name,
                category=self.category,
                description="Deribit 期权（IV/Put-Call/Skew/GEX/到期日，全球 BTC 期权主交易所）",
                supported_symbols=["BTC", "ETH"],
                documentation_url="https://docs.deribit.com/",
            )
        return self._metadata

    def get_supported_data_types(self) -> list[str]:
        return [
            "options_oi", "options_volume", "implied_volatility",
            "put_call_ratio", "skew", "gamma_exposure", "expiration_data",
        ]

    # ---- 内部工具 ----

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> FetchResult:
        """调用 Deribit public API 并解包 jsonrpc 响应。"""
        result = await self._request("GET", path, params=params)
        if not result.success:
            return result

        body = result.data
        if isinstance(body, dict) and "result" in body:
            result.data = body["result"]
        return result

    async def _cached_get(self, key: str, path: str, params: dict[str, Any]) -> FetchResult:
        """带微型缓存的 GET（同一页面周期内多方法复用大响应）。"""
        now = time.monotonic()
        entry = self._cache.get(key)
        if entry and now - entry["cached_at"] < _CACHE_TTL:
            return entry["result"]

        result = await self._get(path, params)
        if result.success:
            self._cache[key] = {"result": result, "cached_at": now}
        return result

    async def _get_book_summary(self, currency: str = "BTC") -> FetchResult:
        """获取期权盘口摘要（所有合约）。"""
        return await self._cached_get(
            f"book_summary:{currency}",
            "/public/get_book_summary_by_currency",
            {"currency": currency, "kind": "option"},
        )

    async def _get_instruments(self, currency: str = "BTC") -> FetchResult:
        """获取期权合约列表。"""
        return await self._cached_get(
            f"instruments:{currency}",
            "/public/get_instruments",
            {"currency": currency, "kind": "option", "expired": "false"},
        )

    @staticmethod
    def _wrap_error(result: FetchResult, message: str) -> FetchResult:
        return FetchResult(
            success=False,
            error=message,
            error_type=result.error_type or ErrorType.DATA_FORMAT,
            provider_name=result.provider_name,
            status_code=result.status_code,
        )

    # ---- 期权 OI ----

    async def get_options_oi(self, symbol: str = "BTC") -> FetchResult:
        """期权未平仓量（Call/Put 分解 + 名义 USD）。"""
        result = await self._get_book_summary(symbol.upper())
        if not result.success:
            return result
        summaries = result.data
        if not isinstance(summaries, list) or not summaries:
            return self._wrap_error(result, "empty book summary")

        call_oi = put_oi = 0.0
        total_usd = 0.0
        index_price: float | None = None
        for row in summaries:
            if not isinstance(row, dict):
                continue
            parsed = _parse_instrument(row.get("instrument_name", ""))
            if not parsed:
                continue
            oi = float(row.get("open_interest") or 0.0)
            if parsed["kind"] == "call":
                call_oi += oi
            else:
                put_oi += oi
            if index_price is None:
                index_price = row.get("underlying_price")
            total_usd += oi * float(row.get("underlying_price") or 0.0)

        ts = utcnow()
        return FetchResult(
            success=True,
            data=options_payload(
                "oi",
                symbol,
                open_interest=call_oi + put_oi,
                call_oi=call_oi,
                put_oi=put_oi,
                open_interest_usd=total_usd,
                timestamp=ts,
                source=self.name,
            ),
            provider_name=self.name,
            fetch_time=ts,
            observation_time=ts,
            quality_status=QualityStatus.VERIFIED,
            raw_response=result.raw_response,
            status_code=result.status_code,
            response_time_ms=result.response_time_ms,
            metadata={"exchange": "deribit", "unit": "btc", "index_price": index_price},
        )

    # ---- 期权成交量 ----

    async def get_options_volume(self, symbol: str = "BTC") -> FetchResult:
        """期权 24h 成交量（Call/Put 分解）。"""
        result = await self._get_book_summary(symbol.upper())
        if not result.success:
            return result
        summaries = result.data
        if not isinstance(summaries, list) or not summaries:
            return self._wrap_error(result, "empty book summary")

        call_vol = put_vol = 0.0
        for row in summaries:
            if not isinstance(row, dict):
                continue
            parsed = _parse_instrument(row.get("instrument_name", ""))
            if not parsed:
                continue
            vol = float(row.get("volume") or 0.0)
            if parsed["kind"] == "call":
                call_vol += vol
            else:
                put_vol += vol

        ts = utcnow()
        return FetchResult(
            success=True,
            data=options_payload(
                "volume",
                symbol,
                volume=call_vol + put_vol,
                call_volume=call_vol,
                put_volume=put_vol,
                timestamp=ts,
                source=self.name,
            ),
            provider_name=self.name,
            fetch_time=ts,
            observation_time=ts,
            quality_status=QualityStatus.VERIFIED,
            raw_response=result.raw_response,
            status_code=result.status_code,
            response_time_ms=result.response_time_ms,
            metadata={"exchange": "deribit", "unit": "btc", "window": "24h"},
        )

    # ---- 隐含波动率 ----

    async def get_implied_volatility(self, symbol: str = "BTC") -> FetchResult:
        """隐含波动率：ATM IV（最近到期）+ DVOL 指数（7d/30d）。"""
        base = symbol.upper()
        summary = await self._get_book_summary(base)
        if not summary.success:
            return summary
        summaries = summary.data
        if not isinstance(summaries, list) or not summaries:
            return self._wrap_error(summary, "empty book summary")

        # 指数价格
        index_price = next(
            (row.get("underlying_price") for row in summaries
             if isinstance(row, dict) and row.get("underlying_price")),
            None,
        )

        # ATM IV：最近到期日中 strike 最接近 index_price 的 call/put 均值
        atm_iv: float | None = None
        nearest_expiry: datetime | None = None
        if index_price:
            dated = [
                (row, _parse_instrument(row.get("instrument_name", "")))
                for row in summaries
                if isinstance(row, dict)
            ]
            dated = [(row, p) for row, p in dated if p]
            if dated:
                nearest_expiry = min(p["expiry"] for _, p in dated)
                nearest = [
                    (row, p) for row, p in dated if p["expiry"] == nearest_expiry
                ]
                nearest.sort(key=lambda rp: abs(rp[1]["strike"] - index_price))
                atm_rows = nearest[:2]
                ivs = [
                    float(row.get("mark_iv") or 0.0)
                    for row, _ in atm_rows
                    if row.get("mark_iv")
                ]
                if ivs:
                    atm_iv = sum(ivs) / len(ivs)

        # DVOL 指数
        bvol30 = await self._get(
            "/public/get_volatility_index_data",
            {"currency": base, "start_timestamp": int((utcnow() - timedelta(minutes=5)).timestamp() * 1000),
             "end_timestamp": int(utcnow().timestamp() * 1000), "resolution": "60"},
        )
        dvol_value: float | None = None
        if bvol30.success and isinstance(bvol30.data, dict):
            closes = bvol30.data.get("data", [])
            if isinstance(closes, list) and closes:
                last = closes[-1]
                if isinstance(last, list) and len(last) >= 5:
                    dvol_value = float(last[4])  # [start, high, low, open, close]

        ts = utcnow()
        return FetchResult(
            success=True,
            data=options_payload(
                "implied_volatility",
                symbol,
                atm_iv=atm_iv,
                dvol_index=dvol_value,
                index_price=index_price,
                nearest_expiry=nearest_expiry.isoformat() if nearest_expiry else None,
                timestamp=ts,
                source=self.name,
            ),
            provider_name=self.name,
            fetch_time=ts,
            observation_time=ts,
            quality_status=QualityStatus.VERIFIED,
            raw_response=summary.raw_response,
            status_code=summary.status_code,
            response_time_ms=summary.response_time_ms,
            metadata={"exchange": "deribit", "unit": "decimal(1=100%)"},
        )

    # ---- Put/Call Ratio ----

    async def get_put_call_ratio(self, symbol: str = "BTC") -> FetchResult:
        """Put/Call Ratio（成交量与未平仓量两个口径）。"""
        oi_result = await self.get_options_oi(symbol)
        vol_result = await self.get_options_volume(symbol)
        if not oi_result.success and not vol_result.success:
            return oi_result

        call_oi = oi_result.data.get("call_oi") if oi_result.success else None
        put_oi = oi_result.data.get("put_oi") if oi_result.success else None
        call_vol = vol_result.data.get("call_volume") if vol_result.success else None
        put_vol = vol_result.data.get("put_volume") if vol_result.success else None

        oi_ratio = put_oi / call_oi if call_oi else None
        vol_ratio = put_vol / call_vol if call_vol else None

        ts = utcnow()
        return FetchResult(
            success=True,
            data=options_payload(
                "put_call_ratio",
                symbol,
                put_call_oi_ratio=oi_ratio,
                put_call_volume_ratio=vol_ratio,
                put_oi=put_oi,
                call_oi=call_oi,
                put_volume=put_vol,
                call_volume=call_vol,
                timestamp=ts,
                source=self.name,
            ),
            provider_name=self.name,
            fetch_time=ts,
            observation_time=ts,
            quality_status=QualityStatus.VERIFIED,
            metadata={"exchange": "deribit"},
        )

    # ---- 波动率偏斜 ----

    async def get_skew(self, symbol: str = "BTC") -> FetchResult:
        """波动率偏斜：最近到期日的 25-Delta 风险逆转（Call IV - Put IV）。"""
        base = symbol.upper()
        result = await self._get(
            "/public/get_order_book_by_currency",
            {"currency": base, "kind": "option", "depth": 1},
        )
        if not result.success:
            return result
        books = result.data
        if not isinstance(books, list) or not books:
            return self._wrap_error(result, "empty order book")

        index_price = next(
            (b.get("underlying_price") for b in books
             if isinstance(b, dict) and b.get("underlying_price")),
            None,
        )
        if not index_price:
            return self._wrap_error(result, "no underlying price in order book")

        # 收集最近到期日的 (|delta|, mark_iv)
        dated_books = []
        for book in books:
            if not isinstance(book, dict):
                continue
            parsed = _parse_instrument(book.get("instrument_name", ""))
            if parsed and book.get("mark_iv") is not None and book.get("greeks"):
                dated_books.append((parsed, book))
        if not dated_books:
            return self._wrap_error(result, "no instruments with IV/greeks")

        nearest_expiry = min(p["expiry"] for p, _ in dated_books)
        nearest = [(p, b) for p, b in dated_books if p["expiry"] == nearest_expiry]

        call_points = [
            (abs(float(b["greeks"].get("delta") or 0.0)), float(b["mark_iv"]))
            for p, b in nearest
            if p["kind"] == "call" and b["greeks"].get("delta")
        ]
        put_points = [
            (abs(float(b["greeks"].get("delta") or 0.0)), float(b["mark_iv"]))
            for p, b in nearest
            if p["kind"] == "put" and b["greeks"].get("delta")
        ]

        call_iv_25 = _interpolate_iv_at_delta(call_points, 0.25)
        put_iv_25 = _interpolate_iv_at_delta(put_points, 0.25)
        risk_reversal = (
            call_iv_25 - put_iv_25
            if call_iv_25 is not None and put_iv_25 is not None
            else None
        )

        ts = utcnow()
        return FetchResult(
            success=True,
            data=options_payload(
                "skew",
                symbol,
                skew_25d=risk_reversal,
                call_iv_25d=call_iv_25,
                put_iv_25d=put_iv_25,
                expiry=nearest_expiry.isoformat(),
                index_price=index_price,
                timestamp=ts,
                source=self.name,
            ),
            provider_name=self.name,
            fetch_time=ts,
            observation_time=ts,
            quality_status=(
                QualityStatus.VERIFIED if risk_reversal is not None else QualityStatus.ESTIMATED
            ),
            raw_response=result.raw_response,
            status_code=result.status_code,
            response_time_ms=result.response_time_ms,
            metadata={"exchange": "deribit", "method": "25-delta risk reversal (interpolated)"},
        )

    # ---- Gamma 敞口 ----

    async def get_gamma_exposure(self, symbol: str = "BTC") -> FetchResult:
        """Gamma 敞口估算（GEX）。

        约定：做市商通常长 Call / 短 Put（客户买 Put 保护），
        GEX ≈ Σ sign * gamma * OI * spot² * 0.01（call: +1, put: -1）。
        """
        base = symbol.upper()
        result = await self._get(
            "/public/get_order_book_by_currency",
            {"currency": base, "kind": "option", "depth": 1},
        )
        if not result.success:
            return result
        books = result.data
        if not isinstance(books, list) or not books:
            return self._wrap_error(result, "empty order book")

        index_price = next(
            (b.get("underlying_price") for b in books
             if isinstance(b, dict) and b.get("underlying_price")),
            None,
        )
        if not index_price:
            return self._wrap_error(result, "no underlying price")

        gex_total = 0.0
        gex_by_expiry: dict[str, float] = {}
        for book in books:
            if not isinstance(book, dict):
                continue
            parsed = _parse_instrument(book.get("instrument_name", ""))
            greeks = book.get("greeks") or {}
            gamma = greeks.get("gamma")
            oi = float(book.get("open_interest") or 0.0)
            if not parsed or gamma is None or not oi:
                continue
            sign = 1.0 if parsed["kind"] == "call" else -1.0
            gex = sign * float(gamma) * oi * index_price**2 * 0.01
            gex_total += gex
            expiry_key = parsed["expiry"].strftime("%Y-%m-%d")
            gex_by_expiry[expiry_key] = gex_by_expiry.get(expiry_key, 0.0) + gex

        ts = utcnow()
        return FetchResult(
            success=True,
            data=options_payload(
                "gamma_exposure",
                symbol,
                gex=gex_total,
                gex_by_expiry=gex_by_expiry,
                spot=index_price,
                timestamp=ts,
                source=self.name,
            ),
            provider_name=self.name,
            fetch_time=ts,
            observation_time=ts,
            quality_status=QualityStatus.ESTIMATED,  # 基于做市商持仓惯例的估算
            raw_response=result.raw_response,
            status_code=result.status_code,
            response_time_ms=result.response_time_ms,
            metadata={
                "exchange": "deribit",
                "unit": "usd_per_1pct_move",
                "note": "dealer-long-call/short-put convention",
            },
        )

    # ---- 到期日数据 ----

    async def get_expiration_data(self, symbol: str = "BTC") -> FetchResult:
        """到期日列表 + 各到期日合约数与 OI 分布。"""
        base = symbol.upper()
        instruments = await self._get_instruments(base)
        if not instruments.success:
            return instruments
        if not isinstance(instruments.data, list) or not instruments.data:
            return self._wrap_error(instruments, "empty instruments list")

        # OI 按到期日聚合
        oi_by_expiry: dict[str, float] = {}
        summary = await self._get_book_summary(base)
        if summary.success and isinstance(summary.data, list):
            for row in summary.data:
                if not isinstance(row, dict):
                    continue
                parsed = _parse_instrument(row.get("instrument_name", ""))
                if not parsed:
                    continue
                key = parsed["expiry"].strftime("%Y-%m-%d")
                oi_by_expiry[key] = oi_by_expiry.get(key, 0.0) + float(
                    row.get("open_interest") or 0.0
                )

        expiries: dict[str, dict[str, Any]] = {}
        for inst in instruments.data:
            if not isinstance(inst, dict):
                continue
            parsed = _parse_instrument(inst.get("instrument_name", ""))
            if not parsed:
                continue
            key = parsed["expiry"].strftime("%Y-%m-%d")
            entry = expiries.setdefault(
                key,
                {"expiry": key, "contracts": 0, "call_count": 0, "put_count": 0, "open_interest": None},
            )
            entry["contracts"] += 1
            entry["call_count" if parsed["kind"] == "call" else "put_count"] += 1
            if key in oi_by_expiry:
                entry["open_interest"] = oi_by_expiry[key]

        expiry_list = sorted(expiries.values(), key=lambda e: e["expiry"])
        ts = utcnow()
        return FetchResult(
            success=True,
            data=options_payload(
                "expiration",
                symbol,
                expirations=expiry_list,
                timestamp=ts,
                source=self.name,
            ),
            provider_name=self.name,
            fetch_time=ts,
            observation_time=ts,
            quality_status=QualityStatus.VERIFIED,
            raw_response=instruments.raw_response,
            status_code=instruments.status_code,
            response_time_ms=instruments.response_time_ms,
            metadata={"exchange": "deribit", "count": len(expiry_list)},
        )


__all__ = ["DeribitProvider"]
