"""周期引擎因子计算。

每个因子函数独立评分（-1 到 +1，-1=极度看跌，+1=极度看涨），
并返回 :class:`EngineEvidence` 证据。当数据源不可用时，
因子返回 (0.0, evidence) 且 evidence.confidence=0，引擎自动降级。

因子清单（架构文档 §1.1）：
1. 价格趋势：MA200 斜率、价格相对 MA200 位置、距 ATH 百分比
2. 链上行为：MVRV、NUPL、SOPR、LTH/STH 供应比
3. 资金流：交易所净流入/流出、ETF 资金流
4. 衍生品：Funding Rate 均值、OI 变化
5. 宏观：DXY 趋势、利率方向
6. 情绪：Fear & Greed Index
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
    "calculate_price_trend_factor",
    "calculate_onchain_factor",
    "calculate_capital_flow_factor",
    "calculate_derivative_factor",
    "calculate_macro_factor",
    "calculate_sentiment_factor",
    "calculate_valuation_anchor_factor",
    "CYCLE_FACTOR_WEIGHTS",
]

#: 各因子默认权重（Σ=1，架构文档 §1.1）
CYCLE_FACTOR_WEIGHTS: dict[str, float] = {
    "price_trend": 0.25,
    "onchain": 0.20,
    "capital_flow": 0.15,
    "derivative": 0.12,
    "macro": 0.10,
    "sentiment": 0.08,
    "valuation_anchor": 0.10,
}


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #

def _utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _extract_value(data: Any, *keys: str) -> float | None:
    """从 dict / 嵌套结构中提取第一个可用数值。"""
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
        # 递归搜索 "value" / "price" 等常见键
        for k in ("value", "price", "data"):
            if k in data and isinstance(data[k], dict):
                return _extract_value(data[k], *keys)
    return None


def _closes(candles: list[dict]) -> list[float]:
    """从 K 线字典列表提取收盘价序列。"""
    out: list[float] = []
    for c in candles:
        v = c.get("close")
        if v is not None:
            try:
                f = float(v)
                if f == f:
                    out.append(f)
            except (TypeError, ValueError):
                pass
    return out


def _sma(values: list[float], period: int) -> list[float | None]:
    """简单移动平均。"""
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    window_sum = sum(values[:period])
    result[period - 1] = window_sum / period
    for i in range(period, len(values)):
        window_sum += values[i] - values[i - period]
        result[i] = window_sum / period
    return result


def _unavailable_evidence(factor: str, source: str, reason: str) -> EngineEvidence:
    """构造数据不可用的证据（confidence=0，引擎自动降级）。"""
    return EngineEvidence(
        factor=factor,
        value=None,
        interpretation=f"{factor}数据暂不可用（{reason}），该因子不参与本次判断",
        weight=0.0,
        supports="neutral",
        confidence=0.0,
        data_source=source,
        quality_status="INVALID",
    )


# --------------------------------------------------------------------------- #
# 1. 价格趋势因子
# --------------------------------------------------------------------------- #

async def calculate_price_trend_factor(
    market_service: Any,
    as_of: datetime | None = None,
) -> tuple[float, EngineEvidence]:
    """价格趋势因子（MA200 斜率 + 价格位置 + 距 ATH）。

    评分逻辑：
    - 价格 > MA200 且 MA200 上行 → 正向（最高 +1）
    - 价格 < MA200 且 MA200 下行 → 负向（最低 -1）
    - 距 ATH 越近越正向，深度回撤负向
    """
    as_of = _utc(as_of)
    start = as_of - timedelta(days=500)
    try:
        result = await market_service.get_ohlcv(
            symbol="BTCUSDT", interval="1d", start=start, end=as_of, limit=500
        )
    except Exception as exc:
        logger.warning(f"[cycle] 价格趋势因子获取 K 线失败: {exc!r}")
        return 0.0, _unavailable_evidence("价格趋势", "market_service", str(exc))

    if not result.success or not isinstance(result.data, list) or len(result.data) < 200:
        reason = result.error or "K 线数据不足 200 根"
        return 0.0, _unavailable_evidence("价格趋势", "market_service", reason)

    closes = _closes(result.data)
    if len(closes) < 200:
        return 0.0, _unavailable_evidence("价格趋势", "market_service", "有效收盘价不足")

    ma200 = _sma(closes, 200)
    current_price = closes[-1]
    ma200_now = ma200[-1]
    ma200_prev = ma200[-30] if len(ma200) >= 30 and ma200[-30] is not None else ma200_now

    # MA200 斜率（30 日变化率）
    slope = safe_div(ma200_now - ma200_prev, ma200_prev, 0.0) if ma200_prev else 0.0
    # 价格相对 MA200 位置
    price_vs_ma = safe_div(current_price - ma200_now, ma200_now, 0.0) if ma200_now else 0.0
    # 距 ATH（使用历史最高收盘价近似）
    ath = max(closes) if closes else current_price
    dist_ath = safe_div(current_price - ath, ath, 0.0)

    # 综合评分
    slope_score = clamp(slope * 8.0, -1.0, 1.0)  # 斜率 ±12.5% 映射到 ±1
    position_score = clamp(price_vs_ma * 2.5, -1.0, 1.0)  # 乖离 ±40% 映射到 ±1
    ath_score = clamp(dist_ath * 2.0, -1.0, 1.0)  # 回撤 -50% 映射到 -1

    score = clamp(slope_score * 0.4 + position_score * 0.35 + ath_score * 0.25)
    supports = "bullish" if score > 0.1 else ("bearish" if score < -0.1 else "neutral")

    # 历史分位（当前价格在历史收盘中的分位）
    pct = percentile_rank(closes, current_price)

    if score > 0.3:
        interp = f"价格站在 200 日均线上方（乖离 {price_vs_ma:+.1%}），长期均线上行，趋势偏多"
    elif score < -0.3:
        interp = f"价格跌破 200 日均线（乖离 {price_vs_ma:+.1%}），长期均线走弱，趋势偏空"
    else:
        interp = f"价格在 200 日均线附近震荡（乖离 {price_vs_ma:+.1%}），趋势方向不明"

    evidence = EngineEvidence(
        factor="价格趋势",
        value={
            "price": round(current_price, 2),
            "ma200": round(ma200_now, 2) if ma200_now else None,
            "slope_30d": round(slope, 4),
            "price_vs_ma200": round(price_vs_ma, 4),
            "distance_from_ath": round(dist_ath, 4),
        },
        interpretation=interp,
        weight=CYCLE_FACTOR_WEIGHTS["price_trend"],
        supports=supports,
        confidence=min(1.0, len(closes) / 365) * quality_coefficient(result.quality_status),
        data_source="market_service/ohlcv",
        historical_percentile=round(pct, 1) if pct is not None else None,
        indicator_code="tech.sma",
        quality_status=result.quality_status.value if hasattr(result.quality_status, "value") else str(result.quality_status),
    )
    return score, evidence


# --------------------------------------------------------------------------- #
# 2. 链上因子
# --------------------------------------------------------------------------- #

async def calculate_onchain_factor(
    onchain_service: Any,
    as_of: datetime | None = None,
) -> tuple[float, EngineEvidence]:
    """链上因子（MVRV + NUPL + SOPR + LTH/STH 供应比综合）。

    评分逻辑：
    - MVRV < 1 → 深度低估（看涨），MVRV > 3.5 → 极度高估（看跌）
    - NUPL < 0 → 全网亏损（底部特征），NUPL > 0.75 → 顶部特征
    - SOPR < 1 → 割肉离场（熊市），SOPR > 1 → 获利了结（牛市）
    - LTH 供应上升 → 吸筹（看涨）
    """
    as_of = _utc(as_of)
    sub_scores: list[float] = []
    sub_weights: list[float] = []
    details: dict[str, Any] = {}
    gaps: list[str] = []
    qualities: list[str] = []

    # MVRV
    try:
        mvrv_res = await onchain_service.get_mvrv(as_of)
        if mvrv_res.success:
            mvrv = _extract_value(mvrv_res.data, "value", "mvrv")
            if mvrv is not None:
                details["mvrv"] = round(mvrv, 4)
                qualities.append(
                    mvrv_res.quality_status.value
                    if hasattr(mvrv_res.quality_status, "value")
                    else str(mvrv_res.quality_status)
                )
                # MVRV 评分：<1 → +1（底部），1~2 → 中性偏多，2~3.5 → 中性偏空，>3.5 → -1
                if mvrv < 1.0:
                    s = clamp(0.5 + (1.0 - mvrv) * 1.5, -1, 1)
                elif mvrv < 2.0:
                    s = clamp(0.5 - (mvrv - 1.0) * 0.5, -1, 1)
                elif mvrv < 3.5:
                    s = clamp(0.0 - (mvrv - 2.0) * 0.4, -1, 1)
                else:
                    s = clamp(-0.6 - (mvrv - 3.5) * 0.4, -1, 1)
                sub_scores.append(s)
                sub_weights.append(0.35)
            else:
                gaps.append("MVRV 值为空")
        else:
            gaps.append(f"MVRV: {mvrv_res.error or '获取失败'}")
    except Exception as exc:
        gaps.append(f"MVRV 异常: {exc}")

    # NUPL
    try:
        nupl_res = await onchain_service.get_nupl(as_of)
        if nupl_res.success:
            nupl = _extract_value(nupl_res.data, "value", "nupl")
            if nupl is not None:
                details["nupl"] = round(nupl, 4)
                qualities.append(
                    nupl_res.quality_status.value
                    if hasattr(nupl_res.quality_status, "value")
                    else str(nupl_res.quality_status)
                )
                # NUPL < 0 → 底部（+1），0~0.5 → 中性，0.5~0.75 → 偏空，>0.75 → 顶部（-1）
                if nupl < 0:
                    s = clamp(0.5 + abs(nupl) * 2.0, -1, 1)
                elif nupl < 0.5:
                    s = clamp(0.5 - nupl * 0.8, -1, 1)
                elif nupl < 0.75:
                    s = clamp(0.1 - (nupl - 0.5) * 1.6, -1, 1)
                else:
                    s = clamp(-0.3 - (nupl - 0.75) * 2.8, -1, 1)
                sub_scores.append(s)
                sub_weights.append(0.25)
            else:
                gaps.append("NUPL 值为空")
        else:
            gaps.append(f"NUPL: {nupl_res.error or '获取失败'}")
    except Exception as exc:
        gaps.append(f"NUPL 异常: {exc}")

    # SOPR
    try:
        sopr_res = await onchain_service.get_sopr(as_of)
        if sopr_res.success:
            sopr = _extract_value(sopr_res.data, "value", "sopr", "sma7")
            if sopr is not None:
                details["sopr"] = round(sopr, 4)
                qualities.append(
                    sopr_res.quality_status.value
                    if hasattr(sopr_res.quality_status, "value")
                    else str(sopr_res.quality_status)
                )
                # SOPR < 1 → 割肉（-0.5 熊市特征但接近底部时反转），> 1 → 获利
                s = clamp((sopr - 1.0) * 4.0, -1, 1)
                sub_scores.append(s)
                sub_weights.append(0.20)
            else:
                gaps.append("SOPR 值为空")
        else:
            gaps.append(f"SOPR: {sopr_res.error or '获取失败'}")
    except Exception as exc:
        gaps.append(f"SOPR 异常: {exc}")

    # LTH/STH 供应比
    try:
        lth_res = await onchain_service.get_lth_supply(as_of)
        sth_res = await onchain_service.get_sth_supply(as_of)
        lth = _extract_value(lth_res.data, "value") if lth_res.success else None
        sth = _extract_value(sth_res.data, "value") if sth_res.success else None
        if lth is not None and sth is not None and sth > 0:
            ratio = lth / sth
            details["lth_sth_ratio"] = round(ratio, 4)
            qualities.append("VERIFIED")
            # LTH/STH 比越高 → 筹码集中在长期持有者（看涨）
            # 历史区间大约 1.5~4.0
            s = clamp((ratio - 2.5) * 0.6, -1, 1)
            sub_scores.append(s)
            sub_weights.append(0.20)
        else:
            gaps.append("LTH/STH 供应数据不完整")
    except Exception as exc:
        gaps.append(f"LTH/STH 异常: {exc}")

    if not sub_scores:
        return 0.0, _unavailable_evidence(
            "链上行为", "onchain_service", "; ".join(gaps) or "全部链上指标不可用"
        )

    score = clamp(sum(s * w for s, w in zip(sub_scores, sub_weights)) / sum(sub_weights))
    supports = "bullish" if score > 0.1 else ("bearish" if score < -0.1 else "neutral")
    q_status = "STALE" if any(q == "STALE" for q in qualities) else "VERIFIED"
    coverage = sum(sub_weights)  # 最大 1.0

    mvrv_v = details.get("mvrv")
    if score > 0.3:
        interp = "链上指标显示筹码处于积累状态，长期持有者占比上升，市场底部特征明显"
    elif score < -0.3:
        interp = "链上指标显示获利盘丰厚、短期持有者占比高，市场处于派发或过热阶段"
    else:
        interp = "链上指标处于中性区间，筹码分布未显示极端信号"

    evidence = EngineEvidence(
        factor="链上行为",
        value=details,
        interpretation=interp,
        weight=CYCLE_FACTOR_WEIGHTS["onchain"],
        supports=supports,
        confidence=clamp(coverage * quality_coefficient(q_status)),
        data_source="onchain_service",
        indicator_code="onchain.mvrv",
        quality_status=q_status,
    )
    return score, evidence


# --------------------------------------------------------------------------- #
# 3. 资金流因子
# --------------------------------------------------------------------------- #

async def calculate_capital_flow_factor(
    onchain_service: Any,
    etf_service: Any,
    as_of: datetime | None = None,
) -> tuple[float, EngineEvidence]:
    """资金流因子（交易所净流入/流出 + ETF 资金流）。

    评分逻辑：
    - 交易所净流出（币提到冷钱包）→ 看涨
    - 交易所净流入（币提到交易所准备卖）→ 看跌
    - ETF 持续净流入 → 看涨
    """
    as_of = _utc(as_of)
    sub_scores: list[float] = []
    sub_weights: list[float] = []
    details: dict[str, Any] = {}
    gaps: list[str] = []

    # 交易所净流量
    try:
        flow_res = await onchain_service.get_exchange_netflow(as_of)
        if flow_res.success:
            net_btc = _extract_value(flow_res.data, "net_flow_btc", "netflow", "value")
            net_usd = _extract_value(flow_res.data, "net_flow_usd")
            if net_btc is not None:
                details["exchange_netflow_btc"] = round(net_btc, 2)
                if net_usd is not None:
                    details["exchange_netflow_usd"] = round(net_usd, 0)
                # 净流出（负值）→ 看涨；净流入（正值）→ 看跌
                # 典型日流量 ±5000 BTC
                s = clamp(-net_btc / 3000.0, -1, 1)
                sub_scores.append(s)
                sub_weights.append(0.5)
            else:
                gaps.append("交易所净流量值为空")
        else:
            gaps.append(f"交易所净流量: {flow_res.error or '获取失败'}")
    except Exception as exc:
        gaps.append(f"交易所净流量异常: {exc}")

    # ETF 资金流（7 日累计）
    if etf_service is not None:
        try:
            etf_res = await etf_service.get_net_flow(period="7d", end_date=as_of)
            if etf_res.success:
                etf_flow = _extract_value(etf_res.data, "net_flow_usd", "total", "value")
                if etf_flow is not None:
                    details["etf_7d_netflow_usd"] = round(etf_flow, 0)
                    # 典型周流量 ±5 亿 USD
                    s = clamp(etf_flow / 5e8, -1, 1)
                    sub_scores.append(s)
                    sub_weights.append(0.5)
                else:
                    gaps.append("ETF 资金流值为空")
            else:
                gaps.append(f"ETF 资金流: {etf_res.error or '获取失败'}")
        except Exception as exc:
            gaps.append(f"ETF 资金流异常: {exc}")
    else:
        gaps.append("ETF 服务未配置")

    if not sub_scores:
        return 0.0, _unavailable_evidence(
            "资金流向", "onchain/etf", "; ".join(gaps) or "资金流数据不可用"
        )

    score = clamp(sum(s * w for s, w in zip(sub_scores, sub_weights)) / sum(sub_weights))
    supports = "bullish" if score > 0.1 else ("bearish" if score < -0.1 else "neutral")
    coverage = sum(sub_weights)

    if score > 0.2:
        interp = "资金持续流入（交易所净流出 + ETF 净申购），增量资金入场迹象明显"
    elif score < -0.2:
        interp = "资金持续流出（交易所净流入 + ETF 净赎回），资金撤离压力较大"
    else:
        interp = "资金流向中性，未出现明显的增量或撤离信号"

    evidence = EngineEvidence(
        factor="资金流向",
        value=details,
        interpretation=interp,
        weight=CYCLE_FACTOR_WEIGHTS["capital_flow"],
        supports=supports,
        confidence=clamp(coverage * 0.9),
        data_source="onchain_service/etf_service",
        quality_status="VERIFIED" if not gaps else "ESTIMATED",
    )
    return score, evidence


# --------------------------------------------------------------------------- #
# 4. 衍生品因子
# --------------------------------------------------------------------------- #

async def calculate_derivative_factor(
    derivatives_service: Any,
    as_of: datetime | None = None,
) -> tuple[float, EngineEvidence]:
    """衍生品因子（Funding Rate + OI 变化）。

    评分逻辑：
    - Funding 温和为正 → 健康多头（看涨）
    - Funding 极端为正 → 多头拥挤（反向看跌）
    - Funding 深度为负 → 空头拥挤（反向看涨，逼空风险）
    - OI 快速上升 + 价格上涨 → 趋势确认
    """
    as_of = _utc(as_of)
    sub_scores: list[float] = []
    sub_weights: list[float] = []
    details: dict[str, Any] = {}
    gaps: list[str] = []

    # Funding Rate
    try:
        fr_res = await derivatives_service.get_funding_rate("BTC/USDT")
        if fr_res.success:
            fr = _extract_value(fr_res.data, "funding_rate", "value", "rate")
            if fr is not None:
                # 年化（8h 结算 → ×3×365）
                annualized = fr * 3 * 365
                details["funding_rate"] = round(fr, 6)
                details["funding_annualized"] = round(annualized, 4)
                # 温和正（0~15% 年化）→ 看涨；极端正（>30%）→ 过热看跌；负 → 逼空看涨
                if annualized < -0.10:
                    s = clamp(0.3 + abs(annualized) * 2.0, -1, 1)  # 逼空反弹
                elif annualized < 0.05:
                    s = 0.1  # 中性偏冷
                elif annualized < 0.20:
                    s = clamp(0.2 + (annualized - 0.05) * 3.0, -1, 1)  # 健康多头
                elif annualized < 0.40:
                    s = clamp(0.65 - (annualized - 0.20) * 3.0, -1, 1)  # 开始过热
                else:
                    s = clamp(0.05 - (annualized - 0.40) * 2.0, -1, 1)  # 极端过热
                sub_scores.append(s)
                sub_weights.append(0.55)
            else:
                gaps.append("Funding Rate 值为空")
        else:
            gaps.append(f"Funding: {fr_res.error or '获取失败'}")
    except Exception as exc:
        gaps.append(f"Funding 异常: {exc}")

    # Open Interest
    try:
        oi_res = await derivatives_service.get_open_interest("BTC/USDT")
        if oi_res.success:
            oi_usd = _extract_value(oi_res.data, "open_interest_usd", "open_interest", "value")
            if oi_usd is not None:
                details["open_interest_usd"] = round(oi_usd, 0)
                # OI 绝对值评分需要历史对比，这里用简化逻辑：
                # OI > 200 亿 → 杠杆较高（偏空），OI < 50 亿 → 杠杆低（偏多）
                if oi_usd > 3e10:
                    s = -0.5
                elif oi_usd > 2e10:
                    s = -0.2
                elif oi_usd > 1e10:
                    s = 0.1
                else:
                    s = 0.3
                sub_scores.append(s)
                sub_weights.append(0.45)
            else:
                gaps.append("OI 值为空")
        else:
            gaps.append(f"OI: {oi_res.error or '获取失败'}")
    except Exception as exc:
        gaps.append(f"OI 异常: {exc}")

    if not sub_scores:
        return 0.0, _unavailable_evidence(
            "衍生品", "derivatives_service", "; ".join(gaps) or "衍生品数据不可用"
        )

    score = clamp(sum(s * w for s, w in zip(sub_scores, sub_weights)) / sum(sub_weights))
    supports = "bullish" if score > 0.1 else ("bearish" if score < -0.1 else "neutral")
    coverage = sum(sub_weights)

    fr_ann = details.get("funding_annualized")
    if fr_ann is not None and fr_ann > 0.30:
        interp = f"资金费率年化 {fr_ann:.0%}，多头拥挤度较高，短期存在回调风险"
    elif fr_ann is not None and fr_ann < -0.10:
        interp = f"资金费率年化 {fr_ann:.0%}，空头拥挤，存在逼空反弹可能"
    elif score > 0.1:
        interp = "衍生品市场杠杆水平健康，多头动能温和"
    elif score < -0.1:
        interp = "衍生品市场显示杠杆过度或空头主导，短期压力较大"
    else:
        interp = "衍生品市场处于中性状态，杠杆水平正常"

    evidence = EngineEvidence(
        factor="衍生品",
        value=details,
        interpretation=interp,
        weight=CYCLE_FACTOR_WEIGHTS["derivative"],
        supports=supports,
        confidence=clamp(coverage * 0.85),
        data_source="derivatives_service",
        indicator_code="deriv.funding_rate",
        quality_status="VERIFIED" if not gaps else "ESTIMATED",
    )
    return score, evidence


# --------------------------------------------------------------------------- #
# 5. 宏观因子
# --------------------------------------------------------------------------- #

async def calculate_macro_factor(
    macro_service: Any,
    as_of: datetime | None = None,
) -> tuple[float, EngineEvidence]:
    """宏观因子（DXY 趋势 + 利率方向 + 流动性）。

    评分逻辑：
    - DXY 走弱 → 风险资产利好（看涨）
    - 利率下行/宽松 → 看涨
    - 全球流动性改善 → 看涨
    """
    as_of = _utc(as_of)
    sub_scores: list[float] = []
    sub_weights: list[float] = []
    details: dict[str, Any] = {}
    gaps: list[str] = []

    # DXY
    try:
        dxy_res = await macro_service.get_dxy(as_of)
        if dxy_res.success:
            dxy = _extract_value(dxy_res.data, "value", "dxy")
            if dxy is not None:
                details["dxy"] = round(dxy, 2)
                # DXY 100 为中性基准，< 95 弱美元（看涨），> 110 强美元（看跌）
                s = clamp((102.0 - dxy) * 0.08, -1, 1)
                sub_scores.append(s)
                sub_weights.append(0.40)
            else:
                gaps.append("DXY 值为空")
        else:
            gaps.append(f"DXY: {dxy_res.error or '获取失败'}")
    except Exception as exc:
        gaps.append(f"DXY 异常: {exc}")

    # 联邦基金利率
    try:
        rate_res = await macro_service.get_fed_rate(as_of)
        if rate_res.success:
            rate = _extract_value(rate_res.data, "value", "fed_rate")
            if rate is not None:
                details["fed_rate"] = round(rate, 2)
                # 低利率（< 2%）→ 宽松（看涨）；高利率（> 5%）→ 紧缩（看跌）
                s = clamp((3.5 - rate) * 0.25, -1, 1)
                sub_scores.append(s)
                sub_weights.append(0.35)
            else:
                gaps.append("利率值为空")
        else:
            gaps.append(f"利率: {rate_res.error or '获取失败'}")
    except Exception as exc:
        gaps.append(f"利率异常: {exc}")

    # M2 / 流动性
    try:
        m2_res = await macro_service.get_m2(as_of)
        if m2_res.success:
            m2 = _extract_value(m2_res.data, "value", "m2")
            if m2 is not None:
                details["m2"] = round(m2, 0)
                # M2 增长（需要历史对比，简化：使用固定基准）
                # 这里给一个中性偏多的分数，因为 M2 长期增长
                sub_scores.append(0.15)
                sub_weights.append(0.25)
            else:
                gaps.append("M2 值为空")
        else:
            gaps.append(f"M2: {m2_res.error or '获取失败'}")
    except Exception as exc:
        gaps.append(f"M2 异常: {exc}")

    if not sub_scores:
        return 0.0, _unavailable_evidence(
            "宏观环境", "macro_service", "; ".join(gaps) or "宏观数据不可用"
        )

    score = clamp(sum(s * w for s, w in zip(sub_scores, sub_weights)) / sum(sub_weights))
    supports = "bullish" if score > 0.1 else ("bearish" if score < -0.1 else "neutral")
    coverage = sum(sub_weights)

    dxy_v = details.get("dxy")
    rate_v = details.get("fed_rate")
    if score > 0.2:
        interp = "宏观环境偏友好（美元走弱/利率下行），利好风险资产"
    elif score < -0.2:
        interp = "宏观环境偏紧缩（美元走强/利率高企），风险资产承压"
    else:
        interp = "宏观环境中性，对加密市场影响有限"

    evidence = EngineEvidence(
        factor="宏观环境",
        value=details,
        interpretation=interp,
        weight=CYCLE_FACTOR_WEIGHTS["macro"],
        supports=supports,
        confidence=clamp(coverage * 0.7),  # 宏观数据更新频率低，置信度打折
        data_source="macro_service",
        quality_status="VERIFIED" if not gaps else "ESTIMATED",
    )
    return score, evidence


# --------------------------------------------------------------------------- #
# 6. 情绪因子
# --------------------------------------------------------------------------- #

async def calculate_sentiment_factor(
    sentiment_service: Any,
    as_of: datetime | None = None,
) -> tuple[float, EngineEvidence]:
    """情绪因子（Fear & Greed Index）。

    评分逻辑（反向指标）：
    - 极度恐惧（< 20）→ 底部特征（看涨）
    - 极度贪婪（> 80）→ 顶部特征（看跌）
    - 中性（40~60）→ 0
    """
    as_of = _utc(as_of)
    try:
        fg_res = await sentiment_service.get_fear_greed(as_of)
    except Exception as exc:
        return 0.0, _unavailable_evidence("市场情绪", "sentiment_service", str(exc))

    if not fg_res.success:
        return 0.0, _unavailable_evidence(
            "市场情绪", "sentiment_service", fg_res.error or "获取失败"
        )

    fg = _extract_value(fg_res.data, "value", "fear_greed", "score")
    label = None
    if isinstance(fg_res.data, dict):
        label = fg_res.data.get("label") or fg_res.data.get("sentiment_label")

    if fg is None:
        return 0.0, _unavailable_evidence("市场情绪", "sentiment_service", "F&G 值为空")

    fg = max(0.0, min(100.0, fg))
    # 反向映射：恐惧 → 看涨，贪婪 → 看跌
    # 50 为中性，0 → +1（极度恐惧=底部），100 → -1（极度贪婪=顶部）
    score = clamp((50.0 - fg) / 40.0, -1, 1)
    supports = "bullish" if score > 0.1 else ("bearish" if score < -0.1 else "neutral")

    if fg < 20:
        interp = f"恐惧贪婪指数 {fg:.0f}（极度恐惧），历史上此类恐慌往往对应阶段性底部区域"
    elif fg < 40:
        interp = f"恐惧贪婪指数 {fg:.0f}（恐惧），市场情绪偏冷，卖压可能已大部分释放"
    elif fg <= 60:
        interp = f"恐惧贪婪指数 {fg:.0f}（中性），市场情绪均衡，方向不明"
    elif fg <= 80:
        interp = f"恐惧贪婪指数 {fg:.0f}（贪婪），市场情绪偏热，需警惕追高风险"
    else:
        interp = f"恐惧贪婪指数 {fg:.0f}（极度贪婪），历史上此类狂热往往对应阶段性顶部区域"

    q_status = (
        fg_res.quality_status.value
        if hasattr(fg_res.quality_status, "value")
        else str(fg_res.quality_status)
    )

    evidence = EngineEvidence(
        factor="市场情绪",
        value={"fear_greed": round(fg, 1), "label": label},
        interpretation=interp,
        weight=CYCLE_FACTOR_WEIGHTS["sentiment"],
        supports=supports,
        confidence=quality_coefficient(q_status),
        data_source="sentiment_service",
        historical_percentile=round(fg, 1),  # F&G 本身即 0-100 分位
        quality_status=q_status,
    )
    return score, evidence


# --------------------------------------------------------------------------- #
# 7. 估值锚因子（来自 Valuation Engine 输出）
# --------------------------------------------------------------------------- #

async def calculate_valuation_anchor_factor(
    valuation_output: Any | None = None,
) -> tuple[float, EngineEvidence]:
    """估值锚因子（消费 Valuation Engine 的输出作为周期定位锚）。

    评分逻辑：
    - 深度低估 → 周期底部区域（看涨）
    - 极端高估 → 周期顶部区域（看跌）
    """
    if valuation_output is None:
        return 0.0, _unavailable_evidence(
            "估值锚", "valuation_engine", "估值引擎输出不可用"
        )

    # 从 ValuationEngine 输出提取 score（-1~1，-1=深度低估，+1=极度高估）
    val_score = getattr(valuation_output, "score", None)
    val_state = getattr(valuation_output, "state", "FAIR")
    if val_score is None:
        return 0.0, _unavailable_evidence("估值锚", "valuation_engine", "估值评分为空")

    # 反向：低估 → 周期看涨，高估 → 周期看跌
    score = clamp(-float(val_score), -1, 1)
    supports = "bullish" if score > 0.1 else ("bearish" if score < -0.1 else "neutral")

    state_cn = {
        "DEEP_UNDERVALUED": "深度低估",
        "UNDERVALUED": "低估",
        "FAIR": "合理",
        "OVERVALUED": "偏高",
        "EXTREME_OVERVALUED": "极端高估",
    }.get(val_state, val_state)

    if score > 0.3:
        interp = f"估值引擎判定当前为「{state_cn}」，从周期角度看处于相对底部区域"
    elif score < -0.3:
        interp = f"估值引擎判定当前为「{state_cn}」，从周期角度看处于相对顶部区域"
    else:
        interp = f"估值引擎判定当前为「{state_cn}」，周期位置中性"

    evidence = EngineEvidence(
        factor="估值锚",
        value={"valuation_state": val_state, "valuation_score": round(float(val_score), 4)},
        interpretation=interp,
        weight=CYCLE_FACTOR_WEIGHTS["valuation_anchor"],
        supports=supports,
        confidence=getattr(valuation_output, "confidence", 0.5),
        data_source="valuation_engine",
    )
    return score, evidence
