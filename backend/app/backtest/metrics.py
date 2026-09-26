"""回测绩效指标计算（对应 14-backtest-architecture.md §4）。

设计约束：
- 全部计算只依赖输入的 Polars DataFrame / 列表，无 IO、无随机性 —— 相同输入必产生相同输出；
- 金额类结果保留 Decimal 精度由调用方负责，本模块内部用 float 做统计计算
  （夏普/回撤等比率指标本身即统计量，float 足够且可复现）；
- equity_curve 约定列：``date``（Date/Datetime）、``value``（组合总市值）、
  ``invested``（累计投入本金）、可选 ``cash`` / ``btc_amount`` / ``btc_price``。

指标口径（写入报告时必须引用）：
- 年化收益率以资金加权 IRR（Money-Weighted）为主指标（定投场景 TWRR 会失真），
  同时输出 TWRR 对照；
- 最大回撤基于每日市值净值序列（市值口径）；
- Sharpe = (日收益率均值 − 无风险利率/365) / 日收益率标准差 × √365。
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import polars as pl

# 一年天数（加密货币全年交易，与 14 号文档 §4.1 口径一致）
DAYS_PER_YEAR = 365
# IRR 牛顿迭代参数
_IRR_MAX_ITER = 200
_IRR_TOL = 1e-10
# "大额回撤"恢复时间统计阈值（§4.1：各次 >20% 回撤的恢复天数列表）
BIG_DRAWDOWN_THRESHOLD = 0.20


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _to_float_series(values: Any) -> list[float]:
    """把任意可迭代数值序列转为 float 列表（Decimal/None 安全）。"""
    out: list[float] = []
    for v in values:
        out.append(float(v) if v is not None else math.nan)
    return out


def _dates_of(equity_curve: pl.DataFrame) -> list[date]:
    """提取 equity_curve 的日期列表（Datetime 列自动转 Date）。"""
    col = equity_curve["date"]
    if isinstance(col.dtype, pl.Datetime):
        col = col.dt.date()
    return col.to_list()


def daily_returns_from_values(values: list[float]) -> list[float]:
    """市值序列的简单日收益率（首元素无前值，不产出）。"""
    rets: list[float] = []
    for prev, cur in zip(values[:-1], values[1:], strict=False):
        if prev and not math.isnan(prev) and not math.isnan(cur) and prev > 0:
            rets.append(cur / prev - 1.0)
        else:
            rets.append(math.nan)
    return rets


def twrr_daily_returns(values: list[float], invested: list[float]) -> list[float]:
    """时间加权日收益率（剔除当日新增投入的影响）。

    r_t = (V_t − F_t) / V_{t-1} − 1，其中 F_t = invested_t − invested_{t-1}。
    """
    rets: list[float] = []
    for i in range(1, len(values)):
        prev_v = values[i - 1]
        flow = invested[i] - invested[i - 1]
        if prev_v and prev_v > 0 and not math.isnan(prev_v):
            rets.append((values[i] - flow) / prev_v - 1.0)
        else:
            rets.append(math.nan)
    return rets


# ---------------------------------------------------------------------------
# IRR（资金加权年化收益率）
# ---------------------------------------------------------------------------

def irr_from_equity_curve(
    dates: list[date],
    invested: list[float],
    final_value: float,
) -> float | None:
    """由累计投入曲线构造现金流并求解 IRR（牛顿法 + 二分兜底）。

    现金流约定：投入增加日为负现金流（金额投入），期末最后一日
    追加 +final_value 作为终值回收。返回年化 IRR（如 0.35 = 35%），
    无有效现金流时返回 None。
    """
    flows: list[tuple[float, float]] = []  # (年化时间, 金额)
    if not dates:
        return None
    t0 = dates[0]
    prev_invested = 0.0
    for d, inv in zip(dates, invested, strict=False):
        if math.isnan(inv):
            continue
        delta = inv - prev_invested
        if abs(delta) > 1e-12:
            flows.append(((d - t0).days / DAYS_PER_YEAR, -delta))
        prev_invested = inv
    # 终值回收（若最后一日同时有投入，两笔现金流可同日共存）
    if final_value and not math.isnan(final_value) and final_value > 0:
        flows.append(((dates[-1] - t0).days / DAYS_PER_YEAR, final_value))
    if len(flows) < 2:
        return None
    return solve_irr(flows)


def solve_irr(flows: list[tuple[float, float]]) -> float | None:
    """求解不规则时间现金流的 IRR。

    Args:
        flows: [(年化时间 t_i, 金额 cf_i)]，投入为负、回收为正。

    Returns:
        年化内部收益率；无法收敛时返回 None。
    """
    if not flows:
        return None

    def npv(rate: float) -> float:
        return sum(cf / (1.0 + rate) ** t for t, cf in flows)

    def dnpv(rate: float) -> float:
        return sum(-t * cf / (1.0 + rate) ** (t + 1.0) for t, cf in flows)

    # 牛顿法（初值 10%）
    rate = 0.1
    for _ in range(_IRR_MAX_ITER):
        if rate <= -0.9999:
            break
        f = npv(rate)
        d = dnpv(rate)
        if abs(d) < 1e-16:
            break
        step = f / d
        new_rate = rate - step
        if new_rate <= -0.9999:  # 越界保护
            new_rate = (rate - 0.9999) / 2.0
        if abs(new_rate - rate) < _IRR_TOL:
            return new_rate
        rate = new_rate
    # 二分兜底：[-0.9999, 100] 区间找符号变化
    lo, hi = -0.9999, 100.0
    f_lo = npv(lo)
    for _ in range(_IRR_MAX_ITER):
        mid = (lo + hi) / 2.0
        f_mid = npv(mid)
        if abs(f_mid) < 1e-9 or (hi - lo) < _IRR_TOL:
            return mid
        if (f_lo > 0) != (f_mid > 0):
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return None


def annualized_return(final_value: float, total_invested: float, days: int) -> float | None:
    """简单年化收益率（期末市值 / 总投入，按持有天数年化）。

    仅用于无中间现金流场景（如一次性买入基准）；定投请用 IRR。
    """
    if total_invested <= 0 or days <= 0 or final_value <= 0:
        return None
    return (final_value / total_invested) ** (DAYS_PER_YEAR / days) - 1.0


# ---------------------------------------------------------------------------
# 回撤
# ---------------------------------------------------------------------------

def max_drawdown(values: list[float]) -> float:
    """最大回撤（正数表示，0.35 = 回撤 35%）。空序列返回 0。"""
    peak = -math.inf
    mdd = 0.0
    for v in values:
        if math.isnan(v):
            continue
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


def max_drawdown_series(values: list[float]) -> list[float]:
    """逐点回撤序列（相对历史峰值，正数表示）。"""
    peak = -math.inf
    out: list[float] = []
    for v in values:
        if math.isnan(v):
            out.append(math.nan)
            continue
        peak = max(peak, v)
        out.append((peak - v) / peak if peak > 0 else 0.0)
    return out


def drawdown_episodes(
    dates: list[date], values: list[float]
) -> list[dict[str, Any]]:
    """识别全部回撤 episode（自峰值回落 → 收复峰值）。

    返回每段：{peak_date, trough_date, recovery_date, depth, duration_days,
    recovery_days, recovered}。duration_days 为峰到收复（或未收复至今）天数。
    """
    episodes: list[dict[str, Any]] = []
    peak = -math.inf
    peak_date: date | None = None
    trough = math.inf
    trough_date: date | None = None
    in_dd = False
    for d, v in zip(dates, values, strict=False):
        if math.isnan(v):
            continue
        if v >= peak:
            if in_dd and peak_date is not None:
                episodes.append({
                    "peak_date": peak_date,
                    "trough_date": trough_date,
                    "recovery_date": d,
                    "depth": (peak - trough) / peak if peak > 0 else 0.0,
                    "duration_days": (d - peak_date).days,
                    "recovery_days": (d - trough_date).days if trough_date else None,
                    "recovered": True,
                })
            peak = v
            peak_date = d
            trough = math.inf
            trough_date = None
            in_dd = False
        else:
            in_dd = True
            if v < trough:
                trough = v
                trough_date = d
    if in_dd and peak_date is not None and dates:
        episodes.append({
            "peak_date": peak_date,
            "trough_date": trough_date,
            "recovery_date": None,
            "depth": (peak - trough) / peak if peak > 0 else 0.0,
            "duration_days": (dates[-1] - peak_date).days,
            "recovery_days": None,
            "recovered": False,
        })
    return episodes


def max_drawdown_duration_days(episodes: list[dict[str, Any]]) -> int:
    """最长回撤持续时间（天）：净值从前高回落到收复前高的最长天数。"""
    if not episodes:
        return 0
    return max(int(e["duration_days"]) for e in episodes)


def recovery_times(
    episodes: list[dict[str, Any]], threshold: float = BIG_DRAWDOWN_THRESHOLD
) -> list[int | None]:
    """各次深度超过 threshold 的回撤的恢复天数列表（未收复为 None）。"""
    return [
        e["recovery_days"] if e["recovered"] else None
        for e in episodes
        if e["depth"] >= threshold
    ]


# ---------------------------------------------------------------------------
# 风险调整收益
# ---------------------------------------------------------------------------

def sharpe_ratio(
    daily_rets: list[float], risk_free_rate: float = 0.02
) -> float | None:
    """年化夏普比率。(mean − rf/365) / std × √365。样本不足返回 None。"""
    rets = [r for r in daily_rets if not math.isnan(r)]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    std = math.sqrt(var)
    if std < 1e-15:
        return None
    daily_rf = risk_free_rate / DAYS_PER_YEAR
    return (mean - daily_rf) / std * math.sqrt(DAYS_PER_YEAR)


def sortino_ratio(
    daily_rets: list[float], risk_free_rate: float = 0.02
) -> float | None:
    """年化索提诺比率：分母仅用下行波动（负收益的 RMS）。"""
    rets = [r for r in daily_rets if not math.isnan(r)]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    daily_rf = risk_free_rate / DAYS_PER_YEAR
    downside = [min(r - daily_rf, 0.0) for r in rets]
    d_var = sum(d * d for d in downside) / len(rets)
    d_std = math.sqrt(d_var)
    if d_std < 1e-15:
        return None
    return (mean - daily_rf) / d_std * math.sqrt(DAYS_PER_YEAR)


def calmar_ratio(
    annual_return_rate: float | None, mdd: float
) -> float | None:
    """卡尔马比率 = 年化收益率 / 最大回撤。"""
    if annual_return_rate is None or mdd <= 1e-12:
        return None
    return annual_return_rate / mdd


# ---------------------------------------------------------------------------
# 交易统计
# ---------------------------------------------------------------------------

def trade_statistics(trades: list[Any]) -> dict[str, Any]:
    """由交易列表（含 pnl / realized_pnl 字段或 dict 键）统计胜率与盈亏比。

    口径：只统计已有实现盈亏（卖出平仓）的交易；纯积累策略（无卖出）
    返回 total_trades 但 win_rate/profit_factor 为 None，调用方应在报告中
    注明「无平仓交易，胜率不适用」。
    """
    pnls: list[float] = []
    total_fees = 0.0
    total_slippage = 0.0
    for t in trades:
        if isinstance(t, dict):
            pnl = t.get("pnl")
            fee = t.get("fee", 0.0)
            slip = t.get("slippage", 0.0)
        else:
            pnl = getattr(t, "pnl", None)
            fee = getattr(t, "fee", 0.0)
            slip = getattr(t, "slippage", 0.0)
        total_fees += float(fee or 0.0)
        total_slippage += float(slip or 0.0)
        if pnl is not None and not (isinstance(pnl, float) and math.isnan(pnl)):
            pnls.append(float(pnl))
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "total_trades": len(trades),
        "closed_trades": len(pnls),
        "win_rate": (len(wins) / len(pnls)) if pnls else None,
        "profit_factor": (
            gross_profit / gross_loss if gross_loss > 1e-12
            else (math.inf if gross_profit > 0 else None)
        ),
        "avg_trade_pnl": (sum(pnls) / len(pnls)) if pnls else None,
        "total_fees": total_fees,
        "total_slippage": total_slippage,
    }


def yearly_returns(dates: list[date], values: list[float]) -> dict[int, float]:
    """自然年收益率表 {年份: 收益率}。

    某年收益率 = 年末市值 / 年初基准 − 1；年初基准取上一年最后一个
    市值（首年取该年第一个市值）。
    """
    if not dates:
        return {}
    by_year: dict[int, tuple[float, float, float | None]] = {}
    prev_year_end: float | None = None
    for d, v in zip(dates, values, strict=False):
        if math.isnan(v):
            continue
        entry = by_year.get(d.year)
        if entry is None:
            by_year[d.year] = (v, v, prev_year_end)
        else:
            by_year[d.year] = (entry[0], v, entry[2])
        prev_year_end = v
    out: dict[int, float] = {}
    for year, (first_v, last_v, base) in sorted(by_year.items()):
        b = base if base and base > 0 else first_v
        if b and b > 0:
            out[year] = last_v / b - 1.0
    return out


# ---------------------------------------------------------------------------
# 汇总入口
# ---------------------------------------------------------------------------

def calculate_all(
    equity_curve: pl.DataFrame,
    trades: list[Any] | None = None,
    risk_free_rate: float = 0.02,
    initial_capital: float = 0.0,
) -> dict[str, Any]:
    """计算全部回测绩效指标（§4.1 汇总指标）。

    Args:
        equity_curve: 含 ``date`` / ``value`` / ``invested`` 列的净值曲线；
            可选 ``btc_amount`` / ``avg_cost`` 列（缺省时相关指标为 None）。
        trades: 交易列表（SimTrade / dict），用于胜率、手续费统计。
        risk_free_rate: 年化无风险利率（默认 2%，PIT 3M 国债缺失时的标注默认值）。
        initial_capital: 期初一次性投入本金（计入第 0 日现金流）。

    Returns:
        指标字典，键与 14 号文档 §4.1 对齐。
    """
    trades = trades or []
    if equity_curve.height == 0:
        return {
            "total_return": None, "annualized_return": None, "annualized_return_twrr": None,
            "max_drawdown": 0.0, "max_drawdown_duration": 0, "recovery_time": [],
            "sharpe_ratio": None, "sortino_ratio": None, "calmar_ratio": None,
            "win_rate": None, "profit_factor": None, "total_trades": 0,
            "total_fees": 0.0, "best_year": None, "worst_year": None,
            "yearly_returns": {}, "final_value": None, "final_btc": None,
            "avg_cost": None, "total_invested": 0.0, "days": 0,
        }

    dates = _dates_of(equity_curve)
    values = _to_float_series(equity_curve["value"].to_list())
    invested = (
        _to_float_series(equity_curve["invested"].to_list())
        if "invested" in equity_curve.columns
        else [0.0] * len(dates)
    )
    # 期初一次性资金计入现金流（t=0 投入 initial_capital）
    if initial_capital > 0:
        invested = [inv + initial_capital for inv in invested]

    final_value = values[-1]
    total_invested = invested[-1] if invested else 0.0
    total_return = (final_value / total_invested - 1.0) if total_invested > 0 else None

    days = (dates[-1] - dates[0]).days if len(dates) > 1 else 0
    irr = irr_from_equity_curve(dates, invested, final_value)
    twrr_rets = twrr_daily_returns(values, invested)
    # TWRR 年化：几何连乘
    twrr_annual: float | None = None
    valid_twrr = [r for r in twrr_rets if not math.isnan(r)]
    if valid_twrr and days > 0:
        growth = 1.0
        for r in valid_twrr:
            growth *= 1.0 + r
        if growth > 0:
            twrr_annual = growth ** (DAYS_PER_YEAR / days) - 1.0

    mdd = max_drawdown(values)
    episodes = drawdown_episodes(dates, values)
    simple_rets = daily_returns_from_values(values)

    yr = yearly_returns(dates, values)
    best_year = max(yr.items(), key=lambda kv: kv[1]) if yr else None
    worst_year = min(yr.items(), key=lambda kv: kv[1]) if yr else None

    final_btc: float | None = None
    avg_cost: float | None = None
    if "btc_amount" in equity_curve.columns:
        btc_series = _to_float_series(equity_curve["btc_amount"].to_list())
        final_btc = btc_series[-1] if btc_series else None
    if "avg_cost" in equity_curve.columns:
        ac = _to_float_series(equity_curve["avg_cost"].to_list())
        avg_cost = next((x for x in reversed(ac) if not math.isnan(x) and x > 0), None)

    trade_stats = trade_statistics(trades)
    # 主年化口径：资金加权 IRR；无中间现金流时退化为简单年化
    primary_annual = irr
    if primary_annual is None and total_invested > 0:
        primary_annual = annualized_return(final_value, total_invested + initial_capital, days)

    return {
        "total_return": total_return,
        "annualized_return": primary_annual,          # 资金加权 IRR（主指标）
        "annualized_return_twrr": twrr_annual,        # 时间加权对照
        "max_drawdown": mdd,
        "max_drawdown_duration": max_drawdown_duration_days(episodes),
        "recovery_time": recovery_times(episodes),
        "sharpe_ratio": sharpe_ratio(twrr_rets or simple_rets, risk_free_rate),
        "sortino_ratio": sortino_ratio(twrr_rets or simple_rets, risk_free_rate),
        "calmar_ratio": calmar_ratio(primary_annual, mdd),
        "win_rate": trade_stats["win_rate"],
        "profit_factor": trade_stats["profit_factor"],
        "total_trades": trade_stats["total_trades"],
        "avg_trade_pnl": trade_stats["avg_trade_pnl"],
        "total_fees": trade_stats["total_fees"],
        "total_slippage": trade_stats["total_slippage"],
        "best_year": {"year": best_year[0], "return": best_year[1]} if best_year else None,
        "worst_year": {"year": worst_year[0], "return": worst_year[1]} if worst_year else None,
        "yearly_returns": {str(y): r for y, r in yr.items()},
        "final_value": final_value,
        "final_btc": final_btc,
        "avg_cost": avg_cost,
        "total_invested": total_invested,
        "days": days,
    }


def lump_sum_benchmark(
    dates: list[date], prices: list[float], total_invested: float
) -> float | None:
    """BTC 一次性买入基准收益率（§4.1 对照指标，DCA 有效性对照，必出）。

    同区间首日以等额资金一次性买入并持有至期末。
    """
    if not dates or total_invested <= 0:
        return None
    valid = [
        (d, p) for d, p in zip(dates, prices, strict=False)
        if p and not math.isnan(p) and p > 0
    ]
    if len(valid) < 2:
        return None
    first_price = valid[0][1]
    last_price = valid[-1][1]
    return last_price / first_price - 1.0


def date_range_days(start: date, end: date) -> int:
    """闭区间天数。"""
    return max((end - start).days + 1, 0)


def shift_date(d: date, days: int) -> date:
    """日期偏移辅助（供回放/恢复时间计算复用）。"""
    return d + timedelta(days=days)


class BacktestMetrics:
    """回测绩效指标计算门面（对齐任务契约 §8）。

    以静态方法暴露 :func:`calculate_all` 等核心口径，便于 ``BacktestMetrics.
    calculate_all(...)`` 直接调用；实现全部委托给本模块的纯函数（无 IO、
    无随机、相同输入必产生相同输出）。
    """

    @staticmethod
    def calculate_all(
        equity_curve: pl.DataFrame,
        trades: list[Any] | None = None,
        risk_free_rate: float = 0.02,
        initial_capital: float = 0.0,
    ) -> dict[str, Any]:
        """计算全部绩效指标（委托 :func:`calculate_all`）。"""
        return calculate_all(
            equity_curve, trades=trades, risk_free_rate=risk_free_rate,
            initial_capital=initial_capital,
        )

    @staticmethod
    def max_drawdown(values: list[float]) -> float:
        """最大回撤（委托 :func:`max_drawdown`）。"""
        return max_drawdown(values)

    @staticmethod
    def sharpe_ratio(returns: list[float], risk_free_rate: float = 0.02) -> float | None:
        """夏普比率（委托 :func:`sharpe_ratio`）。"""
        return sharpe_ratio(returns, risk_free_rate)

    @staticmethod
    def lump_sum_benchmark(
        dates: list[date], prices: list[float], total_invested: float
    ) -> float | None:
        """一次性买入基准（委托 :func:`lump_sum_benchmark`）。"""
        return lump_sum_benchmark(dates, prices, total_invested)


__all__ = [
    "BacktestMetrics",
    "calculate_all",
    "solve_irr",
    "irr_from_equity_curve",
    "annualized_return",
    "max_drawdown",
    "max_drawdown_series",
    "drawdown_episodes",
    "max_drawdown_duration_days",
    "recovery_times",
    "sharpe_ratio",
    "sortino_ratio",
    "calmar_ratio",
    "trade_statistics",
    "yearly_returns",
    "lump_sum_benchmark",
    "daily_returns_from_values",
    "twrr_daily_returns",
    "date_range_days",
    "shift_date",
    "DAYS_PER_YEAR",
    "BIG_DRAWDOWN_THRESHOLD",
]
