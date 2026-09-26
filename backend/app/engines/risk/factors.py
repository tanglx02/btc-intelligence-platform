"""风险引擎因子计算（架构文档 §3）。

11 个风险因子，每个独立评分 0-100（0=无风险，100=极端风险）：
1. valuation_risk   — 基于估值引擎输出
2. volatility_risk  — 历史波动率分位
3. leverage_risk    — Funding Rate + OI/市值比
4. funding_risk     — 极端 Funding Rate
5. oi_risk          — OI 快速变化
6. liquidation_risk — 清算密度/级联风险
7. liquidity_risk   — 订单簿深度/Spread
8. macro_risk       — DXY 走强/利率上升
9. onchain_risk     — 交易所大量流入/鲸鱼异动
10. crowding_risk   — 市场一致性过高
11. drawdown_risk   — 当前回撤速度与深度

每个因子返回 (score_0_100 | None, EngineEvidence, details)；
数据不可用时 score=None，引擎降级（跳过该因子、重归一化权重、置信度下降）。
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
    "RISK_DIMENSION_WEIGHTS",
    "FACTOR_TO_DIMENSION",
    "calculate_valuation_risk",
    "calculate_volatility_risk",
    "calculate_leverage_risk",
    "calculate_funding_risk",
    "calculate_oi_risk",
    "calculate_liquidation_risk",
    "calculate_liquidity_risk",
    "calculate_macro_risk",
    "calculate_onchain_risk",
    "calculate_crowding_risk",
    "calculate_drawdown_risk",
]

#: 11 因子 → 7 输出维度的归属（架构文档 §3）
FACTOR_TO_DIMENSION: dict[str, str] = {
    "volatility_risk": "trend_risk",
    "drawdown_risk": "trend_risk",
    "valuation_risk": "valuation_risk",
    "leverage_risk": "leverage_risk",
    "funding_risk": "leverage_risk",
    "oi_risk": "leverage_risk",
    "liquidation_risk": "liquidity_risk",
    "liquidity_risk": "liquidity_risk",
    "crowding_risk": "liquidity_risk",
    "macro_risk": "macro_risk",
    "onchain_risk": "onchain_risk",
}

#: 7 维度 → overall_risk 的权重（架构文档 §3：0.20/0.15/0.20/0.15/0.15/0.15，Σ=1.00）
RISK_DIMENSION_WEIGHTS: dict[str, float] = {
    "trend_risk": 0.20,
    "valuation_risk": 0.15,
    "leverage_risk": 0.20,
    "liquidity_risk": 0.15,
    "macro_risk": 0.15,
    "onchain_risk": 0.15,
}

#: BTC 近似流通量（用于 OI/市值比估算；仅作数量级参考）
_BTC_SUPPLY_APPROX = 19.8e6


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #

def _utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _extract(data: Any, *keys: str) -> float | None:
    """从 ServiceResult.data 提取第一个可用数值。"""
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


def _quality_str(result: Any) -> str:
    qs = getattr(result, "quality_status", None)
    return qs.value if hasattr(qs, "value") else str(qs or "VERIFIED")


def _unavailable(factor: str, source: str, reason: str) -> tuple[None, EngineEvidence, dict]:
    """数据不可用时的降级返回。"""
    ev = EngineEvidence(
        factor=factor,
        value=None,
        interpretation=f"{factor}数据暂不可用（{reason}），该风险因子不参与本次评估",
        weight=0.0,
        supports="neutral",
        confidence=0.0,
        data_source=source,
        quality_status="INVALID",
    )
    return None, ev, {}


def _risk_supports(score: float) -> str:
    """风险分 → 证据方向（高风险=看跌，低风险=中性偏多）。"""
    if score >= 60:
        return "bearish"
    if score <= 25:
        return "bullish"
    return "neutral"


def _interp(factor_cn: str, score: float, detail: str) -> str:
    """按风险分档生成中文解释。"""
    if score >= 85:
        level = "极端风险"
    elif score >= 70:
        level = "很高风险"
    elif score >= 50:
        level = "较高风险"
    elif score >= 30:
        level = "中等风险"
    elif score >= 15:
        level = "较低风险"
    else:
        level = "很低风险"
    return f"{factor_cn}：{level}（{score:.0f}/100）。{detail}"


def _closes(candles: list[dict]) -> list[float]:
    out: list[float] = []
    for c in candles:
        v = c.get("close")
        if v is not None:
            try:
                f = float(v)
                if f > 0:
                    out.append(f)
            except (TypeError, ValueError):
                pass
    return out


# --------------------------------------------------------------------------- #
# 1. 估值风险（消费 ValuationEngine 输出）
# --------------------------------------------------------------------------- #

async def calculate_valuation_risk(
    valuation_output: Any | None = None,
) -> tuple[float | None, EngineEvidence, dict[str, Any]]:
    """估值风险：直接映射估值引擎的综合评分（0-100，越贵风险越高）。"""
    if valuation_output is None or getattr(valuation_output, "confidence", 0) <= 0:
        return _unavailable("估值风险", "valuation_engine", "估值引擎输出不可用")

    val_score = float(getattr(valuation_output, "score", 50.0) or 50.0)
    level = getattr(valuation_output, "state", "FAIR")
    score = clamp(val_score, 0.0, 100.0)

    level_cn = {
        "DEEP_UNDERVALUED": "深度低估", "UNDERVALUED": "低估", "FAIR": "合理",
        "OVERVALUED": "高估", "EXTREME_OVERVALUED": "极度高估",
    }.get(str(level), str(level))
    detail = f"估值引擎判定当前为「{level_cn}」（评分 {val_score:.0f}/100），估值越高回调风险越大"

    ev = EngineEvidence(
        factor="估值风险",
        value={"valuation_level": str(level), "valuation_score": round(val_score, 2)},
        interpretation=_interp("估值风险", score, detail),
        weight=0.12,
        supports=_risk_supports(score),
        confidence=float(getattr(valuation_output, "confidence", 0.5)),
        data_source="valuation_engine",
        indicator_code="engine.valuation",
        quality_status=getattr(valuation_output, "data_quality", "VERIFIED"),
    )
    return round(score, 2), ev, {"valuation_level": str(level), "valuation_score": val_score}


# --------------------------------------------------------------------------- #
# 2. 波动率风险
# --------------------------------------------------------------------------- #

async def calculate_volatility_risk(
    market_service: Any,
    as_of: datetime | None = None,
) -> tuple[float | None, EngineEvidence, dict[str, Any]]:
    """波动率风险：30 日年化波动率在 1 年历史分布中的分位。"""
    obs = _utc(as_of)
    if market_service is None:
        return _unavailable("波动率风险", "market_service", "行情服务未配置")
    try:
        res = await market_service.get_ohlcv(
            symbol="BTCUSDT", interval="1d",
            start=obs - timedelta(days=400), end=obs, limit=400,
        )
    except Exception as exc:
        return _unavailable("波动率风险", "market_service", str(exc))

    if not res.success or not isinstance(res.data, list):
        return _unavailable("波动率风险", "market_service", res.error or "K 线获取失败")

    closes = _closes(res.data)
    if len(closes) < 60:
        return _unavailable("波动率风险", "market_service", "K 线数据不足 60 根")

    # 日收益率 → 滚动 30 日年化波动率序列
    returns = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i - 1] > 0
    ]
    window = 30
    if len(returns) < window + 10:
        return _unavailable("波动率风险", "market_service", "收益率样本不足")

    vol_series: list[float] = []
    for i in range(window, len(returns) + 1):
        chunk = returns[i - window:i]
        mean = sum(chunk) / window
        var = sum((r - mean) ** 2 for r in chunk) / window
        vol_series.append(math.sqrt(var) * math.sqrt(365))

    current_vol = vol_series[-1]
    pct = percentile_rank(vol_series, current_vol) or 50.0
    score = clamp(pct, 0.0, 100.0)

    detail = (
        f"当前 30 日年化波动率 {current_vol:.0%}，"
        f"处于近一年 {pct:.0f}% 分位，波动越剧烈不确定性越高"
    )
    ev = EngineEvidence(
        factor="波动率风险",
        value={"volatility_30d_annualized": round(current_vol, 4)},
        interpretation=_interp("波动率风险", score, detail),
        weight=0.10,
        supports=_risk_supports(score),
        confidence=min(1.0, len(closes) / 365) * quality_coefficient(_quality_str(res)),
        data_source="market_service/ohlcv",
        historical_percentile=round(pct, 1),
        indicator_code="risk.volatility",
        quality_status=_quality_str(res),
    )
    return round(score, 2), ev, {"volatility_30d": current_vol, "percentile": pct}


# --------------------------------------------------------------------------- #
# 3. 杠杆风险（Funding + OI/市值）
# --------------------------------------------------------------------------- #

async def calculate_leverage_risk(
    derivatives_service: Any,
    as_of: datetime | None = None,
) -> tuple[float | None, EngineEvidence, dict[str, Any]]:
    """杠杆风险：Funding 年化水平（50%）+ OI/市值比（50%）。"""
    obs = _utc(as_of)
    if derivatives_service is None:
        return _unavailable("杠杆风险", "derivatives_service", "衍生品服务未配置")

    funding_annualized: float | None = None
    oi_usd: float | None = None
    qualities: list[str] = []

    try:
        fr_res = await derivatives_service.get_funding_rate("BTC/USDT")
        if fr_res.success:
            fr = _extract(fr_res.data, "funding_rate")
            if fr is not None:
                funding_annualized = fr * 3 * 365  # 8h 一期 → 年化
                qualities.append(_quality_str(fr_res))
    except Exception as exc:
        logger.debug(f"[risk] Funding 获取失败: {exc!r}")

    try:
        oi_res = await derivatives_service.get_open_interest("BTC/USDT")
        if oi_res.success:
            oi_usd = _extract(oi_res.data, "open_interest_usd", "open_interest")
            if oi_usd is not None:
                qualities.append(_quality_str(oi_res))
    except Exception as exc:
        logger.debug(f"[risk] OI 获取失败: {exc!r}")

    if funding_annualized is None and oi_usd is None:
        return _unavailable("杠杆风险", "derivatives_service", "Funding 与 OI 均不可用")

    sub_scores: list[float] = []
    sub_weights: list[float] = []

    # Funding 年化：0→20 分，±10%→40，±30%→65，±60%→85，≥100%→100
    if funding_annualized is not None:
        a = abs(funding_annualized)
        if a <= 0.10:
            s = 20.0 + a / 0.10 * 20.0
        elif a <= 0.30:
            s = 40.0 + (a - 0.10) / 0.20 * 25.0
        elif a <= 0.60:
            s = 65.0 + (a - 0.30) / 0.30 * 20.0
        else:
            s = min(100.0, 85.0 + (a - 0.60) / 0.40 * 15.0)
        sub_scores.append(clamp(s, 0, 100))
        sub_weights.append(0.5)

    # OI/市值比：假设流通量约 19.8M BTC，用 OI/(price×supply) 估算
    # 比例 <1%→20 分，2%→45，4%→70，≥6%→95
    if oi_usd is not None:
        # 数量级参考：OI ~ $20-60B，市值 ~ $1-2T → 比例 1%-6%
        # 无实时价格时用固定市值近似（1.2T）
        mcap_approx = 1.2e12
        ratio = safe_div(oi_usd, mcap_approx, 0.0)
        if ratio <= 0.01:
            s = ratio / 0.01 * 20.0
        elif ratio <= 0.02:
            s = 20.0 + (ratio - 0.01) / 0.01 * 25.0
        elif ratio <= 0.04:
            s = 45.0 + (ratio - 0.02) / 0.02 * 25.0
        else:
            s = min(100.0, 70.0 + (ratio - 0.04) / 0.02 * 25.0)
        sub_scores.append(clamp(s, 0, 100))
        sub_weights.append(0.5)
    else:
        ratio = None

    total_w = sum(sub_weights) or 1.0
    score = clamp(sum(s * w for s, w in zip(sub_scores, sub_weights)) / total_w, 0, 100)

    parts = []
    if funding_annualized is not None:
        parts.append(f"资金费率年化 {funding_annualized:+.0%}")
    if oi_usd is not None:
        parts.append(f"未平仓合约约 ${oi_usd / 1e9:.0f}B")
    detail = "，".join(parts) + "，杠杆水平越高，连锁清算的燃料越足"

    ev = EngineEvidence(
        factor="杠杆风险",
        value={
            "funding_annualized": round(funding_annualized, 4) if funding_annualized is not None else None,
            "open_interest_usd": oi_usd,
            "oi_mcap_ratio": round(ratio, 4) if ratio is not None else None,
        },
        interpretation=_interp("杠杆风险", score, detail),
        weight=0.12,
        supports=_risk_supports(score),
        confidence=(0.8 if len(sub_scores) == 2 else 0.6)
        * (sum(quality_coefficient(q) for q in qualities) / len(qualities) if qualities else 0.5),
        data_source="derivatives_service/funding+oi",
        indicator_code="deriv.funding_rate",
        quality_status=qualities[0] if qualities else None,
    )
    return round(score, 2), ev, {
        "funding_annualized": funding_annualized,
        "open_interest_usd": oi_usd,
        "oi_mcap_ratio": ratio,
    }


# --------------------------------------------------------------------------- #
# 4. 极端资金费率风险
# --------------------------------------------------------------------------- #

async def calculate_funding_risk(
    derivatives_service: Any,
    as_of: datetime | None = None,
) -> tuple[float | None, EngineEvidence, dict[str, Any]]:
    """资金费率风险：仅在极端水平时给出高分（单边杠杆过热的直接信号）。"""
    obs = _utc(as_of)
    if derivatives_service is None:
        return _unavailable("资金费率风险", "derivatives_service", "衍生品服务未配置")
    try:
        res = await derivatives_service.get_funding_rate("BTC/USDT")
    except Exception as exc:
        return _unavailable("资金费率风险", "derivatives_service", str(exc))

    if not res.success:
        return _unavailable("资金费率风险", "derivatives_service", res.error or "获取失败")

    fr = _extract(res.data, "funding_rate")
    if fr is None:
        return _unavailable("资金费率风险", "derivatives_service", "Funding Rate 值为空")

    annualized = fr * 3 * 365
    a = abs(annualized)
    # 正常区间（±11%≈0.01%/8h）→ 低风险；极端（>50% 年化）→ 高分
    if a <= 0.11:
        score = 10.0 + a / 0.11 * 15.0
    elif a <= 0.30:
        score = 25.0 + (a - 0.11) / 0.19 * 25.0
    elif a <= 0.60:
        score = 50.0 + (a - 0.30) / 0.30 * 25.0
    else:
        score = min(100.0, 75.0 + (a - 0.60) / 0.40 * 25.0)
    score = clamp(score, 0, 100)

    side = "多头付费给空头（多头拥挤）" if fr > 0 else "空头付费给多头（空头拥挤）"
    detail = f"资金费率年化 {annualized:+.0%}，{side}" + ("，处于极端水平" if a > 0.5 else "")

    ev = EngineEvidence(
        factor="资金费率风险",
        value={"funding_rate": fr, "annualized": round(annualized, 4)},
        interpretation=_interp("资金费率风险", score, detail),
        weight=0.08,
        supports=_risk_supports(score),
        confidence=0.85 * quality_coefficient(_quality_str(res)),
        data_source="derivatives_service/funding",
        indicator_code="deriv.funding_rate",
        quality_status=_quality_str(res),
    )
    return round(score, 2), ev, {"funding_rate": fr, "annualized": annualized}


# --------------------------------------------------------------------------- #
# 5. OI 快速变化风险
# --------------------------------------------------------------------------- #

async def calculate_oi_risk(
    derivatives_service: Any,
    market_service: Any = None,
    as_of: datetime | None = None,
) -> tuple[float | None, EngineEvidence, dict[str, Any]]:
    """OI 风险：近期 OI 变化速度（快速堆积或快速去化都放大波动风险）。"""
    obs = _utc(as_of)
    if derivatives_service is None:
        return _unavailable("OI 变化风险", "derivatives_service", "衍生品服务未配置")

    oi_now: float | None = None
    oi_prev: float | None = None
    try:
        res = await derivatives_service.get_open_interest("BTC/USDT")
        if res.success:
            oi_now = _extract(res.data, "open_interest_usd", "open_interest")
    except Exception as exc:
        logger.debug(f"[risk] OI 当前值获取失败: {exc!r}")

    # 历史 OI（本地衍生品序列表，如可用）
    try:
        loader = getattr(derivatives_service, "_load_local_range", None)
        if loader is None and hasattr(derivatives_service, "get_open_interest_history"):
            hist_res = await derivatives_service.get_open_interest_history(
                "BTC/USDT", obs - timedelta(days=7), obs
            )
            if hist_res.success and isinstance(hist_res.data, list) and len(hist_res.data) >= 2:
                vals = [
                    _extract(r, "open_interest_usd", "open_interest")
                    for r in hist_res.data
                ]
                vals = [v for v in vals if v is not None]
                if len(vals) >= 2:
                    oi_prev = vals[0]
                    oi_now = oi_now or vals[-1]
    except Exception as exc:
        logger.debug(f"[risk] OI 历史获取失败: {exc!r}")

    if oi_now is None or oi_prev is None or oi_prev <= 0:
        return _unavailable("OI 变化风险", "derivatives_service", "OI 历史序列不可用，无法计算变化速度")

    change_7d = safe_div(oi_now - oi_prev, oi_prev, 0.0)
    a = abs(change_7d)
    # 7d 变化：±5% 内→低分，±15%→50，±30%→75，≥50%→95
    if a <= 0.05:
        score = 15.0 + a / 0.05 * 15.0
    elif a <= 0.15:
        score = 30.0 + (a - 0.05) / 0.10 * 20.0
    elif a <= 0.30:
        score = 50.0 + (a - 0.15) / 0.15 * 25.0
    else:
        score = min(100.0, 75.0 + (a - 0.30) / 0.20 * 20.0)
    score = clamp(score, 0, 100)

    direction = "快速堆积" if change_7d > 0 else "快速去化"
    detail = f"未平仓合约 7 天变化 {change_7d:+.1%}（{direction}），持仓结构不稳定时容易被行情引爆"

    ev = EngineEvidence(
        factor="OI 变化风险",
        value={"oi_now_usd": oi_now, "oi_7d_ago_usd": oi_prev, "change_7d": round(change_7d, 4)},
        interpretation=_interp("OI 变化风险", score, detail),
        weight=0.08,
        supports=_risk_supports(score),
        confidence=0.7,
        data_source="derivatives_service/open_interest",
        indicator_code="deriv.open_interest",
    )
    return round(score, 2), ev, {"oi_change_7d": change_7d}


# --------------------------------------------------------------------------- #
# 6. 清算风险
# --------------------------------------------------------------------------- #

async def calculate_liquidation_risk(
    derivatives_service: Any,
    as_of: datetime | None = None,
) -> tuple[float | None, EngineEvidence, dict[str, Any]]:
    """清算风险：24h 清算总额（清算密度高 = 级联风险正在释放/易再触发）。"""
    obs = _utc(as_of)
    if derivatives_service is None:
        return _unavailable("清算风险", "derivatives_service", "衍生品服务未配置")
    try:
        res = await derivatives_service.get_liquidation("BTC/USDT")
    except Exception as exc:
        return _unavailable("清算风险", "derivatives_service", str(exc))

    if not res.success:
        return _unavailable("清算风险", "derivatives_service", res.error or "获取失败")

    total = _extract(res.data, "liquidation_total_usd", "total_usd", "liquidation_usd")
    if total is None:
        return _unavailable("清算风险", "derivatives_service", "清算数据为空")

    # 清算额分档：<$50M→10，$100M→25，$300M→50，$700M→75，≥$1.5B→95
    m = total / 1e6
    if m <= 50:
        score = 10.0 + m / 50 * 15.0
    elif m <= 100:
        score = 25.0 + (m - 50) / 50 * 25.0
    elif m <= 300:
        score = 50.0 + (m - 100) / 200 * 25.0
    elif m <= 700:
        score = 75.0 + (m - 300) / 400 * 20.0
    else:
        score = min(100.0, 95.0)
    score = clamp(score, 0, 100)

    detail = f"近 24 小时全网清算约 ${m:.0f}M，清算潮会自我强化（级联效应）"
    ev = EngineEvidence(
        factor="清算风险",
        value={"liquidation_24h_usd": total},
        interpretation=_interp("清算风险", score, detail),
        weight=0.08,
        supports=_risk_supports(score),
        confidence=0.75 * quality_coefficient(_quality_str(res)),
        data_source="derivatives_service/liquidation",
        indicator_code="deriv.liquidation",
        quality_status=_quality_str(res),
    )
    return round(score, 2), ev, {"liquidation_24h_usd": total}


# --------------------------------------------------------------------------- #
# 7. 流动性风险（订单簿深度/Spread）
# --------------------------------------------------------------------------- #

async def calculate_liquidity_risk(
    market_service: Any,
    as_of: datetime | None = None,
) -> tuple[float | None, EngineEvidence, dict[str, Any]]:
    """流动性风险：买卖价差 + 订单簿深度（薄流动性下价格易被单笔大单打穿）。"""
    obs = _utc(as_of)
    if market_service is None:
        return _unavailable("流动性风险", "market_service", "行情服务未配置")
    try:
        res = await market_service.get_order_book("BTCUSDT", limit=50)
    except Exception as exc:
        return _unavailable("流动性风险", "market_service", str(exc))

    if not res.success or not isinstance(res.data, dict):
        return _unavailable("流动性风险", "market_service", res.error or "订单簿获取失败")

    bids = res.data.get("bids") or []
    asks = res.data.get("asks") or []
    if not bids or not asks:
        return _unavailable("流动性风险", "market_service", "订单簿为空")

    try:
        best_bid = float(bids[0][0] if isinstance(bids[0], (list, tuple)) else bids[0].get("price", 0))
        best_ask = float(asks[0][0] if isinstance(asks[0], (list, tuple)) else asks[0].get("price", 0))
    except (TypeError, ValueError, IndexError):
        return _unavailable("流动性风险", "market_service", "订单簿格式异常")

    mid = (best_bid + best_ask) / 2
    spread_pct = safe_div(best_ask - best_bid, mid, 0.0) if mid > 0 else 0.0

    def _depth(side: list) -> float:
        total = 0.0
        for item in side[:20]:
            try:
                if isinstance(item, (list, tuple)):
                    total += float(item[0]) * float(item[1])
                else:
                    total += float(item.get("price", 0)) * float(item.get("quantity", 0))
            except (TypeError, ValueError):
                continue
        return total

    bid_depth = _depth(bids)
    ask_depth = _depth(asks)
    depth_usd = bid_depth + ask_depth

    # Spread 评分：<0.01%→10，0.05%→35，0.2%→65，≥0.5%→90
    sp = spread_pct * 100
    if sp <= 0.01:
        s_spread = 10.0
    elif sp <= 0.05:
        s_spread = 10.0 + (sp - 0.01) / 0.04 * 25.0
    elif sp <= 0.2:
        s_spread = 35.0 + (sp - 0.05) / 0.15 * 30.0
    else:
        s_spread = min(90.0, 65.0 + (sp - 0.2) / 0.3 * 25.0)

    # 深度评分（前 20 档双侧合计）：>$50M→10，$20M→30，$5M→60，<$1M→90
    dm = depth_usd / 1e6
    if dm >= 50:
        s_depth = 10.0
    elif dm >= 20:
        s_depth = 10.0 + (50 - dm) / 30 * 20.0
    elif dm >= 5:
        s_depth = 30.0 + (20 - dm) / 15 * 30.0
    elif dm >= 1:
        s_depth = 60.0 + (5 - dm) / 4 * 30.0
    else:
        s_depth = 90.0

    score = clamp(s_spread * 0.4 + s_depth * 0.6, 0, 100)
    detail = f"买卖价差 {spread_pct:.3%}，前 20 档双侧深度约 ${dm:.1f}M"

    ev = EngineEvidence(
        factor="流动性风险",
        value={
            "spread_pct": round(spread_pct, 6),
            "depth_usd": round(depth_usd, 2),
            "bid_ask_imbalance": round(safe_div(bid_depth - ask_depth, bid_depth + ask_depth, 0.0), 4),
        },
        interpretation=_interp("流动性风险", score, detail),
        weight=0.08,
        supports=_risk_supports(score),
        confidence=0.8 * quality_coefficient(_quality_str(res)),
        data_source="market_service/order_book",
        indicator_code="market.order_book",
        quality_status=_quality_str(res),
    )
    return round(score, 2), ev, {
        "spread_pct": spread_pct, "depth_usd": depth_usd,
        "bid_depth": bid_depth, "ask_depth": ask_depth,
    }


# --------------------------------------------------------------------------- #
# 8. 宏观风险
# --------------------------------------------------------------------------- #

async def calculate_macro_risk(
    macro_service: Any,
    as_of: datetime | None = None,
) -> tuple[float | None, EngineEvidence, dict[str, Any]]:
    """宏观风险：DXY 走强 + 利率上行（风险资产的双重逆风）。"""
    obs = _utc(as_of)
    if macro_service is None:
        return _unavailable("宏观风险", "macro_service", "宏观服务未配置")

    sub_scores: list[float] = []
    sub_weights: list[float] = []
    details: dict[str, Any] = {}
    qualities: list[str] = []

    # DXY 30 日动量
    try:
        dxy_now_res = await macro_service.get_dxy(obs)
        dxy_now = _extract(dxy_now_res.data, "value") if dxy_now_res.success else None
        dxy_prev_res = await macro_service.get_dxy(obs - timedelta(days=30))
        dxy_prev = _extract(dxy_prev_res.data, "value") if dxy_prev_res.success else None
        if dxy_now is not None:
            details["dxy"] = dxy_now
            qualities.append(_quality_str(dxy_now_res))
            momentum = safe_div(dxy_now - dxy_prev, dxy_prev, 0.0) if dxy_prev else 0.0
            details["dxy_momentum_30d"] = momentum
            # DXY 30d 涨 >3% → 高风险（美元走强压制风险资产）
            if momentum <= 0:
                s = max(0.0, 20.0 + momentum / 0.03 * 20.0)  # 走弱 → 低风险
            elif momentum <= 0.03:
                s = 20.0 + momentum / 0.03 * 40.0
            else:
                s = min(100.0, 60.0 + (momentum - 0.03) / 0.03 * 40.0)
            sub_scores.append(clamp(s, 0, 100))
            sub_weights.append(0.5)
    except Exception as exc:
        logger.debug(f"[risk] DXY 获取失败: {exc!r}")

    # Fed 利率水平与方向
    try:
        rate_now_res = await macro_service.get_fed_rate(obs)
        rate_now = _extract(rate_now_res.data, "value") if rate_now_res.success else None
        rate_prev_res = await macro_service.get_fed_rate(obs - timedelta(days=90))
        rate_prev = _extract(rate_prev_res.data, "value") if rate_prev_res.success else None
        if rate_now is not None:
            details["fed_rate"] = rate_now
            qualities.append(_quality_str(rate_now_res))
            rising = (rate_prev is not None and rate_now > rate_prev + 0.01)
            # 利率 >5% 且上行 → 高风险；<3.5% 且下行 → 低风险
            s = clamp((rate_now - 3.5) / 2.0 * 50.0 + 25.0, 0, 100)
            if rising:
                s = min(100.0, s + 20.0)
            elif rate_prev is not None and rate_now < rate_prev - 0.01:
                s = max(0.0, s - 15.0)
            details["fed_rate_rising"] = rising
            sub_scores.append(clamp(s, 0, 100))
            sub_weights.append(0.5)
    except Exception as exc:
        logger.debug(f"[risk] Fed 利率获取失败: {exc!r}")

    if not sub_scores:
        return _unavailable("宏观风险", "macro_service", "DXY 与利率数据均不可用")

    total_w = sum(sub_weights) or 1.0
    score = clamp(sum(s * w for s, w in zip(sub_scores, sub_weights)) / total_w, 0, 100)

    parts = []
    if "dxy_momentum_30d" in details:
        parts.append(f"美元指数 30 天{'走强' if details['dxy_momentum_30d'] > 0 else '走弱'} {details['dxy_momentum_30d']:+.1%}")
    if "fed_rate" in details:
        parts.append(f"基准利率 {details['fed_rate']:.2f}%" + ("（上行中）" if details.get("fed_rate_rising") else ""))
    detail = "，".join(parts) + "。美元与利率走强通常压制加密等风险资产"

    ev = EngineEvidence(
        factor="宏观风险",
        value=details,
        interpretation=_interp("宏观风险", score, detail),
        weight=0.10,
        supports=_risk_supports(score),
        confidence=(0.75 if len(sub_scores) == 2 else 0.55)
        * (sum(quality_coefficient(q) for q in qualities) / len(qualities) if qualities else 0.5),
        data_source="macro_service/dxy+fed_rate",
        indicator_code="macro.dxy",
        quality_status=qualities[0] if qualities else None,
    )
    return round(score, 2), ev, details


# --------------------------------------------------------------------------- #
# 9. 链上风险（交易所流入/鲸鱼异动）
# --------------------------------------------------------------------------- #

async def calculate_onchain_risk(
    onchain_service: Any,
    as_of: datetime | None = None,
) -> tuple[float | None, EngineEvidence, dict[str, Any]]:
    """链上风险：交易所净流入放大（潜在抛压）+ 鲸鱼异动。"""
    obs = _utc(as_of)
    if onchain_service is None:
        return _unavailable("链上风险", "onchain_service", "链上服务未配置")

    netflow_btc: float | None = None
    whale_score: float | None = None
    qualities: list[str] = []
    details: dict[str, Any] = {}

    # 近 7 日交易所净流入（正=流入交易所=潜在卖压）
    try:
        flows_res = await onchain_service.get_exchange_flows(
            obs - timedelta(days=7), obs
        )
        if flows_res.success and isinstance(flows_res.data, list) and flows_res.data:
            total = 0.0
            for row in flows_res.data:
                v = _extract(row, "net_flow_btc", "netflow_btc", "net_btc")
                if v is not None:
                    total += v
            netflow_btc = total
            details["netflow_7d_btc"] = total
            qualities.append(_quality_str(flows_res))
    except Exception as exc:
        logger.debug(f"[risk] 交易所资金流获取失败: {exc!r}")

    # 鲸鱼活动
    try:
        whale_res = await onchain_service.get_whale_activity(obs)
        if whale_res.success:
            idx = _extract(whale_res.data, "value", "whale_index", "ratio")
            if idx is not None:
                details["whale_activity"] = idx
                qualities.append(_quality_str(whale_res))
                # 鲸鱼指数以 1.0 为常态基准（>2 视为显著异动）
                whale_score = clamp((idx - 1.0) / 1.5 * 60.0 + 20.0, 0, 100)
    except Exception as exc:
        logger.debug(f"[risk] 鲸鱼活动获取失败: {exc!r}")

    if netflow_btc is None and whale_score is None:
        return _unavailable("链上风险", "onchain_service", "资金流与鲸鱼数据均不可用")

    sub_scores: list[float] = []
    sub_weights: list[float] = []
    if netflow_btc is not None:
        # 7d 净流入：-5000 BTC（流出）→10 分，0→30，+3000→60，+8000→85，≥15000→98
        nf = netflow_btc
        if nf <= 0:
            s = max(0.0, 30.0 + nf / 5000 * 20.0)
        elif nf <= 3000:
            s = 30.0 + nf / 3000 * 30.0
        elif nf <= 8000:
            s = 60.0 + (nf - 3000) / 5000 * 25.0
        else:
            s = min(100.0, 85.0 + (nf - 8000) / 7000 * 13.0)
        sub_scores.append(clamp(s, 0, 100))
        sub_weights.append(0.65)
    if whale_score is not None:
        sub_scores.append(whale_score)
        sub_weights.append(0.35)

    total_w = sum(sub_weights) or 1.0
    score = clamp(sum(s * w for s, w in zip(sub_scores, sub_weights)) / total_w, 0, 100)

    parts = []
    if netflow_btc is not None:
        direction = "净流入" if netflow_btc > 0 else "净流出"
        parts.append(f"近 7 天交易所{direction} {abs(netflow_btc):,.0f} BTC")
    if whale_score is not None:
        parts.append("鲸鱼账户出现异动" if whale_score > 55 else "鲸鱼活动正常")
    detail = "，".join(parts) + "。大量币流入交易所通常意味着潜在卖压上升"

    ev = EngineEvidence(
        factor="链上风险",
        value=details,
        interpretation=_interp("链上风险", score, detail),
        weight=0.10,
        supports=_risk_supports(score),
        confidence=(0.75 if len(sub_scores) == 2 else 0.55)
        * (sum(quality_coefficient(q) for q in qualities) / len(qualities) if qualities else 0.5),
        data_source="onchain_service/exchange_flows+whale",
        indicator_code="onchain.exchange_netflow",
        quality_status=qualities[0] if qualities else None,
    )
    return round(score, 2), ev, details


# --------------------------------------------------------------------------- #
# 10. 拥挤度风险（市场一致性）
# --------------------------------------------------------------------------- #

async def calculate_crowding_risk(
    derivatives_service: Any,
    sentiment_service: Any = None,
    as_of: datetime | None = None,
) -> tuple[float | None, EngineEvidence, dict[str, Any]]:
    """拥挤度风险：多空比极端 + 情绪极端（所有人都站在同一边 = 反转燃料）。"""
    obs = _utc(as_of)
    sub_scores: list[float] = []
    sub_weights: list[float] = []
    details: dict[str, Any] = {}
    qualities: list[str] = []

    # 多空比（>2.5 或 <0.6 视为极端一致性）
    if derivatives_service is not None:
        try:
            ls_res = await derivatives_service.get_long_short_ratio("BTC/USDT")
            if ls_res.success:
                ls = _extract(ls_res.data, "long_short_ratio", "longShortRatio", "ratio")
                if ls is not None and ls > 0:
                    details["long_short_ratio"] = ls
                    qualities.append(_quality_str(ls_res))
                    if 0.8 <= ls <= 2.0:
                        s = 20.0
                    elif ls > 2.0:
                        s = min(100.0, 30.0 + (ls - 2.0) / 1.5 * 55.0)
                    else:
                        s = min(100.0, 30.0 + (0.8 - ls) / 0.5 * 55.0)
                    sub_scores.append(clamp(s, 0, 100))
                    sub_weights.append(0.55)
        except Exception as exc:
            logger.debug(f"[risk] 多空比获取失败: {exc!r}")

    # 情绪极端（F&G ≤10 或 ≥90 都是一致性过高）
    if sentiment_service is not None:
        try:
            fg_res = await sentiment_service.get_fear_greed(obs)
            if fg_res.success:
                fg = _extract(fg_res.data, "value", "normalized_value")
                if fg is not None:
                    details["fear_greed"] = fg
                    qualities.append(_quality_str(fg_res))
                    if fg >= 90:
                        s = min(100.0, 70.0 + (fg - 90) / 10 * 30.0)
                    elif fg >= 75:
                        s = 40.0 + (fg - 75) / 15 * 30.0
                    elif fg <= 10:
                        s = min(100.0, 70.0 + (10 - fg) / 10 * 30.0)
                    elif fg <= 25:
                        s = 40.0 + (25 - fg) / 15 * 30.0
                    else:
                        s = 20.0
                    sub_scores.append(clamp(s, 0, 100))
                    sub_weights.append(0.45)
        except Exception as exc:
            logger.debug(f"[risk] 情绪数据获取失败: {exc!r}")

    if not sub_scores:
        return _unavailable("拥挤度风险", "derivatives+sentiment", "多空比与情绪数据均不可用")

    total_w = sum(sub_weights) or 1.0
    score = clamp(sum(s * w for s, w in zip(sub_scores, sub_weights)) / total_w, 0, 100)

    parts = []
    if "long_short_ratio" in details:
        parts.append(f"多空比 {details['long_short_ratio']:.2f}")
    if "fear_greed" in details:
        fg = details["fear_greed"]
        mood = "极度贪婪" if fg >= 75 else ("极度恐惧" if fg <= 25 else "中性")
        parts.append(f"恐惧贪婪指数 {fg:.0f}（{mood}）")
    detail = "，".join(parts) + "。市场观点越一致，反向挤压的空间越大"

    ev = EngineEvidence(
        factor="拥挤度风险",
        value=details,
        interpretation=_interp("拥挤度风险", score, detail),
        weight=0.08,
        supports=_risk_supports(score),
        confidence=(0.8 if len(sub_scores) == 2 else 0.6)
        * (sum(quality_coefficient(q) for q in qualities) / len(qualities) if qualities else 0.5),
        data_source="derivatives+sentiment",
        indicator_code="deriv.long_short_ratio",
        quality_status=qualities[0] if qualities else None,
    )
    return round(score, 2), ev, details


# --------------------------------------------------------------------------- #
# 11. 回撤风险（速度与深度）
# --------------------------------------------------------------------------- #

async def calculate_drawdown_risk(
    market_service: Any,
    as_of: datetime | None = None,
) -> tuple[float | None, EngineEvidence, dict[str, Any]]:
    """回撤风险：当前回撤深度（60%）+ 30 日回撤速度（40%）。"""
    obs = _utc(as_of)
    if market_service is None:
        return _unavailable("回撤风险", "market_service", "行情服务未配置")
    try:
        res = await market_service.get_ohlcv(
            symbol="BTCUSDT", interval="1d",
            start=obs - timedelta(days=1800), end=obs, limit=1800,
        )
    except Exception as exc:
        return _unavailable("回撤风险", "market_service", str(exc))

    if not res.success or not isinstance(res.data, list):
        return _unavailable("回撤风险", "market_service", res.error or "K 线获取失败")

    closes = _closes(res.data)
    if len(closes) < 60:
        return _unavailable("回撤风险", "market_service", "K 线数据不足")

    ath = max(closes)
    current = closes[-1]
    dd = safe_div(current - ath, ath, 0.0)  # ≤ 0
    dd_30_ago = safe_div(closes[-31] - ath, ath, 0.0) if len(closes) >= 31 else dd
    # 30 日回撤速度（负=回撤正在加深）
    dd_speed = dd - dd_30_ago

    # 深度分：0%→10，-20%→35，-40%→60，-60%→80，-80%→95
    a = abs(dd)
    if a <= 0.20:
        s_depth = 10.0 + a / 0.20 * 25.0
    elif a <= 0.40:
        s_depth = 35.0 + (a - 0.20) / 0.20 * 25.0
    elif a <= 0.60:
        s_depth = 60.0 + (a - 0.40) / 0.20 * 20.0
    else:
        s_depth = min(100.0, 80.0 + (a - 0.60) / 0.20 * 15.0)

    # 速度分：30 天内回撤加深 >25pp → 高分（快速下坠 = 恐慌进行中）
    if dd_speed >= 0:
        s_speed = 15.0  # 回撤在收窄
    elif dd_speed >= -0.10:
        s_speed = 15.0 + abs(dd_speed) / 0.10 * 30.0
    elif dd_speed >= -0.25:
        s_speed = 45.0 + (abs(dd_speed) - 0.10) / 0.15 * 30.0
    else:
        s_speed = min(100.0, 75.0 + (abs(dd_speed) - 0.25) / 0.15 * 25.0)

    score = clamp(s_depth * 0.6 + s_speed * 0.4, 0, 100)
    detail = (
        f"当前距历史最高点回撤 {a:.0%}"
        + (f"，且近 30 天回撤加深 {abs(dd_speed):.0%}（下坠速度快）" if dd_speed < -0.05
           else "，近 30 天回撤趋稳或收窄")
    )

    ev = EngineEvidence(
        factor="回撤风险",
        value={
            "drawdown": round(dd, 4),
            "ath": round(ath, 2),
            "price": round(current, 2),
            "drawdown_speed_30d": round(dd_speed, 4),
        },
        interpretation=_interp("回撤风险", score, detail),
        weight=0.08,
        supports=_risk_supports(score),
        confidence=min(1.0, len(closes) / 730) * quality_coefficient(_quality_str(res)),
        data_source="market_service/ohlcv",
        indicator_code="tech.drawdown_ath",
        quality_status=_quality_str(res),
    )
    return round(score, 2), ev, {
        "drawdown": dd, "ath": ath, "drawdown_speed_30d": dd_speed,
    }
