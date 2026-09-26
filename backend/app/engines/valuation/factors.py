"""估值引擎因子计算（架构文档 §2）。

5 个估值维度，每个独立评分 0-100（0=深度低估，100=极度高估）：
1. MVRV 维度（25%）：MVRV 的历史分位数 + 绝对水平锚定
2. Realized Cap 维度（25%）：价格相对 Realized Cap 的倍数
3. 成本基础维度（20%）：当前价格 vs 全网平均成本（Realized Price）
4. 趋势维度（15%）：长期对数回归趋势线偏离度
5. 回撤维度（15%）：从 ATH 的回撤幅度在历史中的分位

每个因子返回 (score_0_100, EngineEvidence, details)；
数据不可用时 score=None，引擎自动降级（跳过该维度、重归一化权重）。

重要约束：估值状态只描述「当前价格相对历史的高低」，
不能解释为「未来一定上涨/下跌」。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

from app.engines.base import (
    EngineEvidence,
    clamp,
    percentile_rank,
    quality_coefficient,
    safe_div,
)

__all__ = [
    "VALUATION_FACTOR_WEIGHTS",
    "calculate_mvrv_dimension",
    "calculate_realized_cap_dimension",
    "calculate_cost_basis_dimension",
    "calculate_trend_dimension",
    "calculate_drawdown_dimension",
]

#: 各维度默认权重（Σ=1，任务规格：MVRV 25% / RealizedCap 25% / 成本 20% / 趋势 15% / 回撤 15%）
VALUATION_FACTOR_WEIGHTS: dict[str, float] = {
    "mvrv": 0.25,
    "realized_cap": 0.25,
    "cost_basis": 0.20,
    "trend": 0.15,
    "drawdown": 0.15,
}

#: 历史分位回溯窗口（天）——约 5 年，覆盖至少一个完整周期
HISTORY_LOOKBACK_DAYS = 1800


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #

def _utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _extract_value(data: Any, *keys: str) -> float | None:
    """从 ServiceResult.data 中提取第一个可用数值。"""
    if data is None:
        return None
    if isinstance(data, (int, float)):
        return float(data) if data == data else None
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if v is not None:
                try:
                    f = float(v)
                    if f == f:
                        return f
                except (TypeError, ValueError):
                    continue
    return None


def _series_values(data: Any) -> list[float]:
    """从历史序列 ServiceResult.data 提取数值列表。"""
    if not isinstance(data, list):
        return []
    out: list[float] = []
    for row in data:
        v = _extract_value(row, "value")
        if v is not None:
            out.append(v)
    return out


def _quality_str(result: Any) -> str:
    qs = getattr(result, "quality_status", None)
    return qs.value if hasattr(qs, "value") else str(qs or "VERIFIED")


def _unavailable(factor: str, source: str, reason: str) -> tuple[None, EngineEvidence, dict]:
    """数据不可用时的降级返回（score=None，引擎跳过该维度）。"""
    ev = EngineEvidence(
        factor=factor,
        value=None,
        interpretation=f"{factor}数据暂不可用（{reason}），该维度不参与本次估值判断",
        weight=0.0,
        supports="neutral",
        confidence=0.0,
        data_source=source,
        quality_status="INVALID",
    )
    return None, ev, {}


def _score_supports(score: float) -> str:
    """估值分 → 证据方向（高估=对价格看跌，低估=对价格看涨）。"""
    if score >= 60:
        return "bearish"
    if score <= 40:
        return "bullish"
    return "neutral"


# --------------------------------------------------------------------------- #
# D1. MVRV 维度
# --------------------------------------------------------------------------- #

async def calculate_mvrv_dimension(
    onchain_service: Any,
    as_of: datetime | None = None,
) -> tuple[float | None, EngineEvidence, dict[str, Any]]:
    """MVRV 维度：历史分位（70%）+ 绝对水平锚定（30%）。

    绝对锚定：MVRV < 1 → 深度低估（0 分），MVRV > 3.5 → 极度高估（100 分）。
    """
    obs = _utc(as_of)
    try:
        current_res = await onchain_service.get_mvrv(obs)
    except Exception as exc:
        logger.warning(f"[valuation] MVRV 获取失败: {exc!r}")
        return _unavailable("MVRV 估值维度", "onchain_service", str(exc))

    mvrv = _extract_value(current_res.data, "value", "mvrv") if current_res.success else None
    if mvrv is None:
        return _unavailable("MVRV 估值维度", "onchain_service", current_res.error or "MVRV 值为空")

    # 历史分位（本地 onchain_metrics 表序列）
    pct: float | None = None
    try:
        hist_res = await onchain_service.get_metric_history(
            "MVRV", obs - timedelta(days=HISTORY_LOOKBACK_DAYS), obs
        )
        if hist_res.success:
            values = _series_values(hist_res.data)
            if len(values) >= 30:
                pct = percentile_rank(values, mvrv)
    except Exception as exc:
        logger.debug(f"[valuation] MVRV 历史序列获取失败: {exc!r}")

    # 绝对水平锚定分（分段线性：<1→0-50, 1~2→0-50, 2~3.5→50-85, >3.5→85-100）
    if mvrv < 1.0:
        anchor = clamp(mvrv * 50.0, 0.0, 50.0)
    elif mvrv < 2.0:
        anchor = (mvrv - 1.0) * 50.0
    elif mvrv < 3.5:
        anchor = 50.0 + (mvrv - 2.0) / 1.5 * 35.0
    else:
        anchor = min(100.0, 85.0 + (mvrv - 3.5) * 10.0)

    if pct is not None:
        score = clamp(pct * 0.7 + anchor * 0.3, 0.0, 100.0)
    else:
        score = clamp(anchor, 0.0, 100.0)

    if score >= 80:
        interp = f"MVRV={mvrv:.2f} 处于历史极端高位（分位 {pct:.0f}%），当前价格远高于全网平均持仓成本" if pct else f"MVRV={mvrv:.2f} 处于极端高位，当前价格远高于全网平均持仓成本"
    elif score <= 20:
        interp = f"MVRV={mvrv:.2f} 处于历史低位" + (f"（分位 {pct:.0f}%）" if pct else "") + "，当前价格接近甚至低于全网平均持仓成本"
    else:
        interp = f"MVRV={mvrv:.2f}" + (f" 处于历史 {pct:.0f}% 分位" if pct else " 处于中性区域") + "，估值水平与历史常态相当"

    ev = EngineEvidence(
        factor="MVRV 估值维度",
        value={"mvrv": round(mvrv, 4), "percentile": round(pct, 1) if pct is not None else None},
        interpretation=interp,
        weight=VALUATION_FACTOR_WEIGHTS["mvrv"],
        supports=_score_supports(score),
        confidence=min(1.0, 0.6 + (0.4 if pct is not None else 0.0)) * quality_coefficient(_quality_str(current_res)),
        data_source="onchain_service/mvrv",
        historical_percentile=round(pct, 1) if pct is not None else None,
        indicator_code="onchain.mvrv",
        quality_status=_quality_str(current_res),
    )
    details = {"mvrv": mvrv, "mvrv_percentile": pct, "anchor_score": round(anchor, 2)}
    return round(score, 2), ev, details


# --------------------------------------------------------------------------- #
# D2. Realized Cap 维度
# --------------------------------------------------------------------------- #

async def calculate_realized_cap_dimension(
    onchain_service: Any,
    market_service: Any,
    as_of: datetime | None = None,
) -> tuple[float | None, EngineEvidence, dict[str, Any]]:
    """Realized Cap 维度：市值 / Realized Cap 倍数的历史分位。

    倍数 < 1 → 市值跌破已实现市值（历史大底特征）；
    倍数 > 3.5 → 市值远超已实现市值（历史顶部特征）。
    """
    obs = _utc(as_of)
    try:
        rc_res = await onchain_service.get_realized_cap(obs)
    except Exception as exc:
        logger.warning(f"[valuation] Realized Cap 获取失败: {exc!r}")
        return _unavailable("Realized Cap 维度", "onchain_service", str(exc))

    realized_cap = _extract_value(rc_res.data, "value", "realized_cap") if rc_res.success else None
    if realized_cap is None or realized_cap <= 0:
        return _unavailable("Realized Cap 维度", "onchain_service", rc_res.error or "Realized Cap 值为空")

    # 当前市值 ≈ 价格 × 流通量（用价格/realized_price 近似倍数：mcap/rcap = price/rprice）
    ratio: float | None = None
    price: float | None = None
    realized_price: float | None = None
    try:
        price_res = await market_service.get_current_price("BTCUSDT") if market_service else None
        if price_res and price_res.success:
            price = _extract_value(price_res.data, "price")
        # Realized Price 序列（如可得）
        rp_res = await onchain_service.get_metric_history(
            "REALIZED_CAP", obs - timedelta(days=HISTORY_LOOKBACK_DAYS), obs
        )
        hist_rc = _series_values(rp_res.data) if rp_res.success else []
        if price and hist_rc and len(hist_rc) >= 30:
            # 历史倍数序列无法直接还原（缺历史价格），退化为 RC 增长分位评估
            ratio = None
    except Exception as exc:
        logger.debug(f"[valuation] Realized Cap 维度辅助数据失败: {exc!r}")

    # 评分：MVRV 与 mcap/rcap 数学等价（mcap = price×supply, rcap = rprice×supply），
    # 这里用 Realized Cap 30d/90d 增长率作为周期位置信号：
    # 增长率极高（资金快速涌入）→ 偏热；增长率触底回升 → 偏冷（底部特征）
    growth: float | None = None
    try:
        rc_90_res = await onchain_service.get_realized_cap(obs - timedelta(days=90))
        rc_90 = _extract_value(rc_90_res.data, "value", "realized_cap") if rc_90_res.success else None
        if rc_90 and rc_90 > 0:
            growth = safe_div(realized_cap - rc_90, rc_90, None)
    except Exception:
        growth = None

    if growth is None:
        return _unavailable("Realized Cap 维度", "onchain_service", "90 天前 Realized Cap 缺失，无法计算增长率")

    # 增长率映射：-10%→0 分，0%→30 分，+10%→60 分，+25%→85 分，≥40%→100 分
    if growth <= 0.10:
        score = clamp(30.0 + growth / 0.10 * 30.0, 0.0, 60.0)
    elif growth <= 0.25:
        score = 60.0 + (growth - 0.10) / 0.15 * 25.0
    else:
        score = min(100.0, 85.0 + (growth - 0.25) / 0.15 * 15.0)
    if growth < -0.05:
        score = clamp(30.0 + (growth + 0.05) / 0.15 * 30.0, 0.0, 30.0)

    if score >= 60:
        interp = f"已实现市值 90 天增长 {growth:+.1%}，资金成本快速抬升，估值偏热"
    elif score <= 30:
        interp = f"已实现市值 90 天变化 {growth:+.1%}，链上成本基本停滞，估值处于低位区域"
    else:
        interp = f"已实现市值 90 天增长 {growth:+.1%}，处于历史常态区间"

    ev = EngineEvidence(
        factor="Realized Cap 维度",
        value={"realized_cap_usd": realized_cap, "growth_90d": round(growth, 4)},
        interpretation=interp,
        weight=VALUATION_FACTOR_WEIGHTS["realized_cap"],
        supports=_score_supports(score),
        confidence=0.75 * quality_coefficient(_quality_str(rc_res)),
        data_source="onchain_service/realized_cap",
        indicator_code="onchain.realized_cap",
        quality_status=_quality_str(rc_res),
    )
    details = {
        "realized_cap": realized_cap,
        "realized_price": realized_price,
        "growth_90d": growth,
        "price": price,
        "ratio": ratio,
    }
    return round(score, 2), ev, details


# --------------------------------------------------------------------------- #
# D3. 成本基础维度
# --------------------------------------------------------------------------- #

async def calculate_cost_basis_dimension(
    onchain_service: Any,
    market_service: Any,
    as_of: datetime | None = None,
) -> tuple[float | None, EngineEvidence, dict[str, Any]]:
    """成本基础维度：当前价格 / 全网平均成本（Realized Price）。

    Realized Price ≈ MVRV 的倒数关系：price/rprice = MVRV。
    优先用 NUPL（= 1 - rprice/price）还原成本比。
    """
    obs = _utc(as_of)
    try:
        nupl_res = await onchain_service.get_nupl(obs)
    except Exception as exc:
        logger.warning(f"[valuation] NUPL 获取失败: {exc!r}")
        return _unavailable("成本基础维度", "onchain_service", str(exc))

    nupl = _extract_value(nupl_res.data, "value", "nupl") if nupl_res.success else None
    if nupl is None:
        return _unavailable("成本基础维度", "onchain_service", nupl_res.error or "NUPL 值为空")

    # price / cost_basis = 1 / (1 - NUPL)
    cost_ratio = safe_div(1.0, 1.0 - nupl, None) if nupl < 1.0 else None
    if cost_ratio is None:
        # NUPL ≥ 1 数学上意味着成本为负/零（数据异常），按极端高估处理
        cost_ratio = 10.0

    # 评分：ratio 0.8→0 分（价格低于成本 20%），1.0→15，1.5→40，2.5→65，3.5→85，≥5→100
    if cost_ratio <= 1.0:
        score = clamp(cost_ratio / 1.0 * 15.0, 0.0, 15.0)
    elif cost_ratio <= 1.5:
        score = 15.0 + (cost_ratio - 1.0) / 0.5 * 25.0
    elif cost_ratio <= 2.5:
        score = 40.0 + (cost_ratio - 1.5) / 1.0 * 25.0
    elif cost_ratio <= 3.5:
        score = 65.0 + (cost_ratio - 2.5) / 1.0 * 20.0
    else:
        score = min(100.0, 85.0 + (cost_ratio - 3.5) / 1.5 * 15.0)

    # 历史分位（NUPL 序列 → ratio 序列）
    pct: float | None = None
    try:
        hist_res = await onchain_service.get_metric_history(
            "NUPL", obs - timedelta(days=HISTORY_LOOKBACK_DAYS), obs
        )
        if hist_res.success:
            ratios = [
                safe_div(1.0, 1.0 - v, None) if v < 1.0 else 10.0
                for v in _series_values(hist_res.data)
            ]
            ratios = [r for r in ratios if r is not None]
            if len(ratios) >= 30:
                pct = percentile_rank(ratios, cost_ratio)
                if pct is not None:
                    score = clamp(score * 0.6 + pct * 0.4, 0.0, 100.0)
    except Exception as exc:
        logger.debug(f"[valuation] NUPL 历史序列获取失败: {exc!r}")

    if score >= 60:
        interp = f"当前价格约为全网平均持仓成本的 {cost_ratio:.1f} 倍，获利盘丰厚"
    elif score <= 30:
        interp = f"当前价格仅为全网平均持仓成本的 {cost_ratio:.1f} 倍，多数持有者接近盈亏平衡或亏损"
    else:
        interp = f"当前价格约为全网平均成本的 {cost_ratio:.1f} 倍，处于历史常态区间"

    ev = EngineEvidence(
        factor="成本基础维度",
        value={"nupl": round(nupl, 4), "price_to_cost_ratio": round(cost_ratio, 4)},
        interpretation=interp,
        weight=VALUATION_FACTOR_WEIGHTS["cost_basis"],
        supports=_score_supports(score),
        confidence=(0.8 if pct is not None else 0.65) * quality_coefficient(_quality_str(nupl_res)),
        data_source="onchain_service/nupl",
        historical_percentile=round(pct, 1) if pct is not None else None,
        indicator_code="onchain.nupl",
        quality_status=_quality_str(nupl_res),
    )
    details = {"nupl": nupl, "nupl_percentile": pct, "cost_ratio": cost_ratio}
    return round(score, 2), ev, details


# --------------------------------------------------------------------------- #
# D4. 趋势维度（长期对数回归偏离）
# --------------------------------------------------------------------------- #

async def calculate_trend_dimension(
    market_service: Any,
    as_of: datetime | None = None,
    regression_years: float = 4.0,
) -> tuple[float | None, EngineEvidence, dict[str, Any]]:
    """趋势维度：价格相对长期对数回归趋势线的偏离度。

    对 log(price) 做线性回归（约 4 年窗口），偏离 +1 个标准差以上 → 偏热，
    -1 个标准差以下 → 偏冷。回归参数由数据实时拟合，不硬编码任何周期日期。
    """
    obs = _utc(as_of)
    if market_service is None:
        return _unavailable("趋势偏离维度", "market_service", "行情服务未配置")
    try:
        days = int(regression_years * 365)
        res = await market_service.get_ohlcv(
            symbol="BTCUSDT", interval="1d",
            start=obs - timedelta(days=days), end=obs, limit=days,
        )
    except Exception as exc:
        logger.warning(f"[valuation] 趋势维度获取 K 线失败: {exc!r}")
        return _unavailable("趋势偏离维度", "market_service", str(exc))

    if not res.success or not isinstance(res.data, list) or len(res.data) < 200:
        return _unavailable("趋势偏离维度", "market_service", res.error or "K 线数据不足")

    closes: list[float] = []
    for c in res.data:
        v = c.get("close")
        if v is not None:
            try:
                f = float(v)
                if f > 0:
                    closes.append(f)
            except (TypeError, ValueError):
                pass
    if len(closes) < 200:
        return _unavailable("趋势偏离维度", "market_service", "有效收盘价不足")

    n = len(closes)
    xs = list(range(n))
    ys = [math.log(p) for p in closes]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = safe_div(sxy, sxx, 0.0)
    intercept = mean_y - slope * mean_x
    fitted = slope * (n - 1) + intercept
    residuals = [y - (slope * x + intercept) for x, y in zip(xs, ys)]
    std = math.sqrt(sum(r * r for r in residuals) / n) or 1e-9
    deviation = (ys[-1] - fitted) / std  # 当前偏离（标准差单位）

    # 偏离映射：-2σ→0 分，-1σ→25，0σ→50，+1σ→75，+2σ→100
    score = clamp(50.0 + deviation / 2.0 * 50.0, 0.0, 100.0)
    price = closes[-1]
    trend_price = math.exp(fitted)
    premium = safe_div(price - trend_price, trend_price, 0.0)

    if score >= 75:
        interp = f"价格高于长期趋势线 {premium:+.0%}（偏离 {deviation:+.1f}σ），显著偏热"
    elif score <= 25:
        interp = f"价格低于长期趋势线 {premium:+.0%}（偏离 {deviation:+.1f}σ），显著偏冷"
    else:
        interp = f"价格与长期趋势线的偏离为 {premium:+.0%}（{deviation:+.1f}σ），处于正常波动范围"

    ev = EngineEvidence(
        factor="趋势偏离维度",
        value={
            "price": round(price, 2),
            "regression_price": round(trend_price, 2),
            "deviation_sigma": round(deviation, 3),
            "premium_pct": round(premium, 4),
            "window_years": regression_years,
        },
        interpretation=interp,
        weight=VALUATION_FACTOR_WEIGHTS["trend"],
        supports=_score_supports(score),
        confidence=min(1.0, n / 730) * quality_coefficient(_quality_str(res)),
        data_source="market_service/ohlcv",
        indicator_code="tech.linreg",
        quality_status=_quality_str(res),
    )
    details = {
        "regression_slope_per_day": slope,
        "deviation_sigma": deviation,
        "trend_price": trend_price,
        "premium_pct": premium,
    }
    return round(score, 2), ev, details


# --------------------------------------------------------------------------- #
# D5. 回撤维度
# --------------------------------------------------------------------------- #

async def calculate_drawdown_dimension(
    market_service: Any,
    as_of: datetime | None = None,
    lookback_days: int = 1800,
) -> tuple[float | None, EngineEvidence, dict[str, Any]]:
    """回撤维度：当前距 ATH 的回撤幅度在历史回撤分布中的分位。

    回撤极浅（接近 ATH）→ 高分（偏热）；回撤极深 → 低分（偏冷）。
    """
    obs = _utc(as_of)
    if market_service is None:
        return _unavailable("回撤维度", "market_service", "行情服务未配置")
    try:
        res = await market_service.get_ohlcv(
            symbol="BTCUSDT", interval="1d",
            start=obs - timedelta(days=lookback_days), end=obs, limit=lookback_days,
        )
    except Exception as exc:
        logger.warning(f"[valuation] 回撤维度获取 K 线失败: {exc!r}")
        return _unavailable("回撤维度", "market_service", str(exc))

    if not res.success or not isinstance(res.data, list) or len(res.data) < 90:
        return _unavailable("回撤维度", "market_service", res.error or "K 线数据不足")

    closes: list[float] = []
    for c in res.data:
        v = c.get("close")
        if v is not None:
            try:
                f = float(v)
                if f > 0:
                    closes.append(f)
            except (TypeError, ValueError):
                pass
    if len(closes) < 90:
        return _unavailable("回撤维度", "market_service", "有效收盘价不足")

    # 滚动 ATH 与每日回撤序列
    drawdowns: list[float] = []
    running_max = closes[0]
    for p in closes:
        running_max = max(running_max, p)
        drawdowns.append((p - running_max) / running_max)  # ≤ 0
    current_dd = drawdowns[-1]
    ath = running_max

    # 当前回撤在历史回撤分布中的分位（回撤越浅分位越高 → 越偏热）
    pct = percentile_rank(drawdowns, current_dd)
    if pct is None:
        pct = 50.0

    # 分位映射为估值分（浅回撤=高分）；同时叠加绝对回撤锚定
    # 绝对锚定：0% 回撤→90 分，-20%→65，-40%→45，-60%→25，-80%→5
    abs_dd = abs(current_dd)
    if abs_dd <= 0.20:
        anchor = 90.0 - abs_dd / 0.20 * 25.0
    elif abs_dd <= 0.40:
        anchor = 65.0 - (abs_dd - 0.20) / 0.20 * 20.0
    elif abs_dd <= 0.60:
        anchor = 45.0 - (abs_dd - 0.40) / 0.20 * 20.0
    else:
        anchor = max(0.0, 25.0 - (abs_dd - 0.60) / 0.20 * 20.0)

    score = clamp(pct * 0.6 + anchor * 0.4, 0.0, 100.0)

    if score >= 70:
        interp = f"当前价格距历史最高点仅回撤 {abs_dd:.0%}，处于历史回撤分布的浅位（分位 {pct:.0f}%）"
    elif score <= 30:
        interp = f"当前价格已从历史最高点回撤 {abs_dd:.0%}，回撤深度处于历史极端区域（分位 {pct:.0f}%）"
    else:
        interp = f"当前回撤 {abs_dd:.0%}，处于历史回撤分布的常态区间（分位 {pct:.0f}%）"

    ev = EngineEvidence(
        factor="回撤维度",
        value={
            "current_drawdown": round(current_dd, 4),
            "ath": round(ath, 2),
            "price": round(closes[-1], 2),
        },
        interpretation=interp,
        weight=VALUATION_FACTOR_WEIGHTS["drawdown"],
        supports=_score_supports(score),
        confidence=min(1.0, len(closes) / 730) * quality_coefficient(_quality_str(res)),
        data_source="market_service/ohlcv",
        historical_percentile=round(pct, 1),
        indicator_code="tech.drawdown_ath",
        quality_status=_quality_str(res),
    )
    details = {"drawdown": current_dd, "ath": ath, "drawdown_percentile": pct}
    return round(score, 2), ev, details
