"""持仓与收益计算（对应 15-portfolio-architecture.md §2.2 / §2.3）。

口径（与架构文档一致，界面/报告必须注明）：
- avg_cost 采用 **DCA 移动加权平均法**：Σ(买入金额含费) / Σ(买入 BTC 数)；
  卖出不改变 avg_cost，仅减少余额；
- realized_pnl = Σ(卖出金额 − 卖出数量 × avg_cost − 卖出手续费)；
- total_invested 为净投入法：Σ买入(含费) − Σ卖出金额中的本金回收部分
  （本实现简化为：累计买入含费 − 累计卖出净额，下限截断为 0，报告注明）；
- 全部金额使用 Decimal，数量精度 8 位（satoshi），金额精度 2 位。

纯计算模块：无 IO、无状态、相同输入必产生相同输出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional, Protocol

from app.backtest import metrics as bt_metrics

# 精度约定
BTC_QUANT = Decimal("0.00000001")   # satoshi
AMOUNT_QUANT = Decimal("0.01")      # 法币金额
RATE_QUANT = Decimal("0.000001")    # 比率（6 位小数）
PRICE_QUANT = Decimal("0.00000001")  # 价格

_ZERO = Decimal("0")


class TransactionLike(Protocol):
    """交易记录鸭子类型：ORM UserTransaction 或任意同名字段对象。"""

    side: Any
    amount: Decimal
    price: Decimal
    quantity_btc: Decimal
    fee: Decimal


def q_btc(v: Decimal) -> Decimal:
    """按 satoshi 精度量化。"""
    return v.quantize(BTC_QUANT, rounding=ROUND_HALF_UP)


def q_amount(v: Decimal) -> Decimal:
    """按法币金额精度量化。"""
    return v.quantize(AMOUNT_QUANT, rounding=ROUND_HALF_UP)


def q_rate(v: Decimal) -> Decimal:
    """按比率精度量化。"""
    return v.quantize(RATE_QUANT, rounding=ROUND_HALF_UP)


@dataclass
class Holdings:
    """持仓聚合结果（§2.2 计算口径）。"""

    btc_balance: Decimal = _ZERO          # 当前 BTC 余额
    avg_cost: Decimal = _ZERO             # 移动加权平均成本（含费）
    total_cost: Decimal = _ZERO           # 当前持仓成本 = btc_balance × avg_cost
    total_buy_amount: Decimal = _ZERO     # 累计买入金额（含费）
    total_buy_btc: Decimal = _ZERO        # 累计买入 BTC
    total_sell_amount: Decimal = _ZERO    # 累计卖出金额（扣费后净额）
    total_sell_btc: Decimal = _ZERO       # 累计卖出 BTC
    total_invested: Decimal = _ZERO       # 净投入（买含费 − 卖净额，截断 ≥ 0）
    realized_pnl: Decimal = _ZERO         # 已实现盈亏
    total_fees: Decimal = _ZERO           # 累计手续费
    transaction_count: int = 0


@dataclass
class Performance:
    """绩效指标结果（§2.3 实时估值）。"""

    total_invested: Decimal = _ZERO
    current_value: Optional[Decimal] = None       # 当前市值（无价格时 None）
    unrealized_pnl: Optional[Decimal] = None      # 浮动盈亏
    unrealized_pnl_pct: Optional[Decimal] = None  # 浮动盈亏率（小数口径）
    realized_pnl: Decimal = _ZERO
    pnl: Optional[Decimal] = None                 # 累计收益 = 浮动 + 已实现
    pnl_percentage: Optional[Decimal] = None      # 收益率（小数口径）
    avg_cost: Decimal = _ZERO
    btc_balance: Decimal = _ZERO
    max_drawdown: Optional[Decimal] = None        # 基于快照的最大回撤（小数口径）
    current_drawdown: Optional[Decimal] = None    # 当前回撤（小数口径）
    yearly_returns: dict[str, float] = field(default_factory=dict)


class PortfolioCalculator:
    """持仓与收益计算器（全部为纯函数式静态方法）。"""

    @staticmethod
    def calculate_holdings(transactions: list[TransactionLike]) -> Holdings:
        """按时间顺序回放交易，计算当前持仓（移动加权平均成本法）。

        买入：btc_balance 增加，avg_cost = 累计买入(含费) / 累计买入 BTC；
        卖出：btc_balance 减少，realized_pnl += 卖出净额 − 卖出量 × avg_cost，
        avg_cost 不变（口径文档化）。

        Args:
            transactions: 交易列表（调用方须保证按 transaction_time 升序）。

        Returns:
            Holdings 聚合结果。
        """
        h = Holdings()
        cum_buy_amount = _ZERO  # 累计买入金额（含费），卖出不清零 —— 移动加权口径
        cum_buy_btc = _ZERO
        for tx in transactions:
            side = str(getattr(tx, "side", "")).upper()
            # 兼容 ORM ENUM 对象（TradeSide.BUY → "BUY" / "TradeSide.BUY"）
            if "." in side:
                side = side.split(".")[-1]
            amount = Decimal(tx.amount or 0)
            price = Decimal(tx.price or 0)
            qty = Decimal(tx.quantity_btc or 0)
            fee = Decimal(tx.fee or 0)
            if qty == 0 and price > 0 and amount > 0:
                qty = amount / price
            h.transaction_count += 1
            h.total_fees += fee

            if side == "BUY":
                gross = amount + fee
                cum_buy_amount += gross
                cum_buy_btc += qty
                h.total_buy_amount += gross
                h.total_buy_btc += qty
                h.btc_balance = q_btc(h.btc_balance + qty)
                if cum_buy_btc > 0:
                    h.avg_cost = (cum_buy_amount / cum_buy_btc).quantize(
                        PRICE_QUANT, rounding=ROUND_HALF_UP
                    )
            elif side == "SELL":
                net = amount - fee  # 卖出实收
                sell_qty = min(qty, h.btc_balance)
                cost_of_sold = sell_qty * h.avg_cost
                h.realized_pnl += q_amount(net - cost_of_sold)
                h.total_sell_amount += q_amount(net)
                h.total_sell_btc += sell_qty
                h.btc_balance = q_btc(h.btc_balance - sell_qty)
                # 移动加权口径：卖出按比例冲减成本基数，avg_cost 保持不变；
                # 清仓后基数归零，后续重新买入时 avg_cost 从零重建
                if cum_buy_btc > 0:
                    ratio = sell_qty / cum_buy_btc
                    cum_buy_amount = max(cum_buy_amount * (Decimal(1) - ratio), _ZERO)
                    cum_buy_btc = max(cum_buy_btc - sell_qty, _ZERO)
                if h.btc_balance <= 0:
                    h.btc_balance = _ZERO
                    cum_buy_amount = _ZERO
                    cum_buy_btc = _ZERO
                    h.avg_cost = _ZERO
            else:
                raise ValueError(f"未知交易方向: {side!r}")

        h.total_cost = q_amount(h.btc_balance * h.avg_cost)
        h.total_invested = q_amount(
            max(h.total_buy_amount - h.total_sell_amount, _ZERO)
        )
        h.total_buy_amount = q_amount(h.total_buy_amount)
        h.total_sell_amount = q_amount(h.total_sell_amount)
        h.total_fees = q_amount(h.total_fees)
        return h

    @staticmethod
    def calculate_dca_cost(transactions: list[TransactionLike]) -> Decimal:
        """DCA 平均成本 = Σ(买入金额含费) / Σ(买入 BTC 数量)。

        只统计买入交易；无买入时返回 0。
        """
        total_amount = _ZERO
        total_btc = _ZERO
        for tx in transactions:
            side = str(getattr(tx, "side", "")).upper()
            if "." in side:
                side = side.split(".")[-1]
            if side != "BUY":
                continue
            fee = Decimal(tx.fee or 0)
            total_amount += Decimal(tx.amount or 0) + fee
            total_btc += Decimal(tx.quantity_btc or 0)
        if total_btc <= 0:
            return _ZERO
        return (total_amount / total_btc).quantize(PRICE_QUANT, rounding=ROUND_HALF_UP)

    @staticmethod
    def calculate_performance(
        transactions: list[TransactionLike],
        current_price: Optional[Decimal] = None,
        snapshots: Optional[list[Any]] = None,
    ) -> Performance:
        """计算绩效指标（持仓 + 估值 + 回撤）。

        Args:
            transactions: 按时间升序的交易列表。
            current_price: 当前 BTC 价格（None 时市值类指标为 None）。
            snapshots: portfolio_snapshots 行（含 total_value 字段），
                用于最大回撤 / 当前回撤 / 年度收益计算。

        Returns:
            Performance 绩效结果。
        """
        h = PortfolioCalculator.calculate_holdings(transactions)
        p = Performance(
            total_invested=h.total_invested,
            realized_pnl=h.realized_pnl,
            avg_cost=h.avg_cost,
            btc_balance=h.btc_balance,
        )
        if current_price is not None:
            current_price = Decimal(current_price)
            p.current_value = q_amount(h.btc_balance * current_price)
            p.unrealized_pnl = q_amount(p.current_value - h.total_cost)
            if h.total_cost > 0:
                p.unrealized_pnl_pct = q_rate(p.unrealized_pnl / h.total_cost)
            p.pnl = q_amount(p.unrealized_pnl + h.realized_pnl)
            if h.total_invested > 0:
                p.pnl_percentage = q_rate(p.pnl / h.total_invested)

        if snapshots:
            values = [float(s.total_value) for s in snapshots if s.total_value is not None]
            p.max_drawdown = q_rate(Decimal(str(bt_metrics.max_drawdown(values))))
            dd_series = bt_metrics.max_drawdown_series(values)
            if dd_series:
                p.current_drawdown = q_rate(Decimal(str(dd_series[-1])))
            dates = [
                s.observation_time.date()
                for s in snapshots
                if getattr(s, "observation_time", None) is not None
            ]
            p.yearly_returns = {
                str(y): r for y, r in bt_metrics.yearly_returns(dates, values).items()
            }
        return p

    @staticmethod
    def calculate_max_drawdown(snapshots: list[Any]) -> Decimal:
        """最大回撤计算（基于快照市值净值序列，小数口径正数表示）。

        Args:
            snapshots: 含 total_value 字段的快照行列表（按时间升序）。
        """
        values = [float(s.total_value) for s in snapshots if s.total_value is not None]
        return q_rate(Decimal(str(bt_metrics.max_drawdown(values))))

    @staticmethod
    def calculate_current_drawdown(snapshots: list[Any]) -> Decimal:
        """当前回撤：最新市值相对历史峰值的回落幅度（小数口径）。"""
        values = [float(s.total_value) for s in snapshots if s.total_value is not None]
        series = bt_metrics.max_drawdown_series(values)
        if not series:
            return _ZERO
        return q_rate(Decimal(str(series[-1])))
