"""预警上下文构建器 —— 一次扫描批量拉取全部所需数据。

关键性能设计：每轮扫描仅构建一次 :class:`AlertContext`，全部规则共享，
避免逐规则重复拉取价格/指标/引擎状态。

数据缺失或超期（max_age）的字段写入 ``stale_fields``，求值器据此返回
UNKNOWN（数据不可用时不触发，亦不误报）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import select

from app.alerts.schemas import ConditionNode
from app.core.database import get_db_session_ctx
from app.models.engine import CycleState, MarketRegime, RiskScore, ValuationState
from app.models.enums import CandleInterval
from app.models.indicator import IndicatorDefinition, IndicatorValue
from app.models.market import Candle
from app.models.portfolio import PortfolioSnapshot
from app.providers.market.symbol_mapper import to_compact

#: 数据质量等级排序（数值大 = 质量高），用于 min_data_quality 门槛判定
QUALITY_RANK: dict[str, int] = {
    "VERIFIED": 4,
    "ESTIMATED": 3,
    "STALE": 2,
    "CONFLICT": 1,
    "INVALID": 0,
}

#: 计价货币（价格上下文默认按 {BASE}/USDT 构建）
QUOTE_CURRENCY = "USDT"


def quality_rank(status: str | None) -> int:
    """数据质量字符串 -> 等级数值（未知状态按最低档）。"""
    if not status:
        return 0
    return QUALITY_RANK.get(status.upper(), 0)


@dataclass
class AlertContext:
    """单次扫描的全局求值上下文（一次构建，全部规则共享）。"""

    timestamp: datetime
    symbol: str = "BTC"                        # 资产符号（如 BTC）
    pair_symbol: str = "BTC/USDT"              # 交易对规范格式
    prices: dict[str, float] = field(default_factory=dict)           # "BTC/USDT" -> price
    # "BTC/USDT" -> {"1h": -2.1, "24h": -5.3, "7d": ..., "30d": ...}
    price_changes: dict[str, dict[str, float]] = field(default_factory=dict)
    indicators: dict[str, float] = field(default_factory=dict)       # "mvrv" -> 1.32
    # 最近序列（交叉检测用）
    indicator_history: dict[str, list[tuple[datetime, float]]] = field(default_factory=dict)
    indicator_percentiles: dict[str, float] = field(default_factory=dict)    # "mvrv" -> 85.2
    engine_states: dict[str, str] = field(default_factory=dict)      # "cycle" -> "UPTREND"
    engine_prev_states: dict[str, str] = field(default_factory=dict) # 上一状态
    portfolio: dict | None = None              # 持仓数据（user_id 提供时）
    data_quality: dict[str, str] = field(default_factory=dict)       # "price" -> "VERIFIED"
    data_sources: dict[str, str] = field(default_factory=dict)       # "price" -> "binance"
    stale_fields: set[str] = field(default_factory=set)              # 不可用字段
    metadata: dict[str, Any] = field(default_factory=dict)           # 扩展信息（price_sources 等）

    # ---- 求值辅助 ----

    def resolve_value(self, metric: str | None) -> float | None:
        """按指标名解析当前值（indicators 优先，其次 price）。

        返回 None 表示数据不可用（求值应为 UNKNOWN）。
        """
        if not metric or metric in self.stale_fields:
            return None
        if metric in self.indicators:
            return self.indicators[metric]
        if metric == "price":
            return next(iter(self.prices.values()), None)
        if metric in self.prices:
            return self.prices[metric]
        return None

    def resolve_price_change(self, metric: str | None, window_hours: float | None) -> float | None:
        """解析指定窗口的价格涨跌幅（%）。"""
        window_key = f"{window_hours:g}h" if window_hours is not None else "24h"
        changes: dict[str, float] | None = None
        if metric and metric in self.price_changes:
            changes = self.price_changes[metric]
        elif self.pair_symbol in self.price_changes:
            changes = self.price_changes[self.pair_symbol]
        elif self.price_changes:
            changes = next(iter(self.price_changes.values()))
        if changes is None:
            return None
        return changes.get(window_key)

    def quality_of(self, field_name: str) -> str | None:
        """字段对应的数据质量状态（未记录返回 None）。"""
        return self.data_quality.get(field_name)

    @property
    def current_price(self) -> float | None:
        """当前价格（首个交易对）。"""
        return next(iter(self.prices.values()), None)

    def to_market_context(self) -> dict[str, Any]:
        """生成事件 market_context 快照（JSON 可序列化）。"""
        return {
            "timestamp": self.timestamp.isoformat(),
            "symbol": self.symbol,
            "price": self.current_price,
            "price_changes": self.price_changes.get(self.pair_symbol, {}),
            "indicators": dict(self.indicators),
            "indicator_percentiles": dict(self.indicator_percentiles),
            "engine_states": dict(self.engine_states),
            "data_quality": dict(self.data_quality),
            "data_sources": dict(self.data_sources),
        }


class ContextBuilder:
    """批量构建上下文——关键性能：一次扫描所有规则只获取一次数据。"""

    # 数据新鲜度上限（秒），超期标记 stale
    PRICE_MAX_AGE = 300            # 当前价 5 分钟
    INDICATOR_MAX_AGE = 48 * 3600  # 指标多为日频，容忍 2 天
    ENGINE_MAX_AGE = 48 * 3600     # 引擎输出为日频
    #: 涨跌幅窗口（标签 -> 小时数）
    CHANGE_WINDOWS: dict[str, int] = {
        "1h": 1,
        "24h": 24,
        "7d": 24 * 7,
        "30d": 24 * 30,
    }

    def __init__(self, market_service: Any = None) -> None:
        self._market_service = market_service

    # ---- 对外入口 ----

    def extract_required_metrics(self, rules: list) -> set[str]:
        """分析规则需要哪些指标（遍历条件树收集 metric 字段）。"""
        metrics: set[str] = set()
        for rule in rules:
            tree = getattr(rule, "condition_tree", None)
            if not tree:
                continue
            try:
                node = ConditionNode.model_validate(tree)
            except Exception as e:  # noqa: BLE001 - 非法规则跳过，不阻断扫描
                logger.warning(
                    f"AlertContext: 规则 '{getattr(rule, 'rule_name', rule.id)}' "
                    f"条件树非法，跳过: {e}"
                )
                continue
            metrics |= node.collect_metrics()
        return metrics

    async def build(
        self,
        required_metrics: set[str],
        symbol: str = "BTC",
        engines: set[str] | None = None,
        user_id: Any = None,
    ) -> AlertContext:
        """批量构建求值上下文。

        步骤：
        1. 价格：MarketService.get_current_price()（失败回退最新 K 线收盘价）；
        2. 涨跌幅：从 candles 表计算（1h/24h/7d/30d 前收盘对比）；
        3. 指标：indicator_values 表最新值 + 最近 20 条历史（交叉/分位用）；
        4. 引擎状态：cycle/valuation/risk/regime 最新两行；
        5. 数据质量/来源：按数据源标记；
        6. 数据缺失/过期的字段加入 stale_fields。
        """
        now = datetime.now(UTC)
        pair = f"{symbol}/{QUOTE_CURRENCY}"
        ctx = AlertContext(timestamp=now, symbol=symbol, pair_symbol=pair)

        # 1. 当前价格（市场服务优先，K 线回退）
        hourly, daily = await self._load_candle_sets(pair)
        await self._build_prices(ctx, pair, hourly, daily)

        # 2. 涨跌幅（基于 K 线收盘价对比当前价）
        self._build_price_changes(ctx, pair, hourly, daily, now)

        # 3. 指标（非价格类指标）
        indicator_metrics = {
            m for m in required_metrics
            if m != "price" and "/" not in m
        }
        if indicator_metrics:
            await self._load_indicators(ctx, indicator_metrics, symbol)

        # 4. 引擎状态
        if engines:
            await self._load_engine_states(ctx, engines)

        # 5. 持仓上下文（可选）
        if user_id is not None:
            await self._load_portfolio(ctx, user_id)

        return ctx

    # ---- 价格与涨跌幅 ----

    def _get_market_service(self) -> Any:
        """惰性获取 MarketService（ProviderService 未就绪时抛异常由调用方捕获）。"""
        if self._market_service is None:
            from app.services.market_service import MarketService
            from app.services.provider_service import get_provider_service

            self._market_service = MarketService(get_provider_service().manager)
        return self._market_service

    async def _build_prices(
        self,
        ctx: AlertContext,
        pair: str,
        hourly: list[tuple[datetime, float, str]],
        daily: list[tuple[datetime, float, str]],
    ) -> None:
        """填充 prices / price 相关质量与来源。"""
        price_val: float | None = None
        price_quality = "STALE"
        price_source = ""
        price_time: datetime | None = None

        try:
            service = self._get_market_service()
            result = await service.get_current_price(pair)
            if result.success and result.data:
                data = result.data if isinstance(result.data, dict) else {}
                raw_price = data.get("price")
                if raw_price is not None:
                    price_val = float(raw_price)
                    price_source = result.source or ""
                    price_time = result.observation_time
                    price_quality = (
                        result.quality_status.value
                        if hasattr(result.quality_status, "value")
                        else str(result.quality_status)
                    )
                    if result.is_stale:
                        price_quality = "STALE"
                    cross = (result.metadata or {}).get("cross_validation") or {}
                    ctx.metadata["price_sources"] = int(cross.get("sources") or 1)
        except Exception as e:  # noqa: BLE001 - 价格获取失败回退 K 线
            logger.warning(f"AlertContext: 当前价获取失败，回退 K 线收盘: {e}")

        if price_val is None and hourly:
            price_val = hourly[-1][1]
            price_quality = hourly[-1][2]
            price_time = hourly[-1][0]
            price_source = "candles_db"
            ctx.metadata.setdefault("price_sources", 1)

        if price_val is not None:
            ctx.prices[pair] = price_val
            ctx.data_quality["price"] = price_quality
            ctx.data_sources["price"] = price_source
        else:
            ctx.stale_fields.add("price")

        self._mark_stale_if_old(ctx, "price", price_time, self.PRICE_MAX_AGE)

    def _build_price_changes(
        self,
        ctx: AlertContext,
        pair: str,
        hourly: list[tuple[datetime, float, str]],
        daily: list[tuple[datetime, float, str]],
        now: datetime,
    ) -> None:
        """基于 K 线收盘价计算各窗口涨跌幅（%）。"""
        current = ctx.prices.get(pair)
        if current is None:
            return
        changes: dict[str, float] = {}
        for label, hours in self.CHANGE_WINDOWS.items():
            series = hourly if hours <= 24 else daily
            past = self._close_at(series, now - timedelta(hours=hours))
            if past is not None and past != 0:
                changes[label] = round((current - past) / past * 100, 4)
        if changes:
            ctx.price_changes[pair] = changes

    @staticmethod
    def _close_at(series: list[tuple[datetime, float, str]], target: datetime) -> float | None:
        """取时间戳 <= target 的最近一根 K 线收盘价（series 升序）。"""
        for ts, close, _quality in reversed(series):
            ts_utc = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
            if ts_utc <= target:
                return close
        return None

    async def _load_candle_sets(
        self, pair: str
    ) -> tuple[list[tuple[datetime, float, str]], list[tuple[datetime, float, str]]]:
        """加载 1h（近 48 根）与 1d（近 40 根）K 线收盘序列（升序）。"""
        hourly = await self._load_closes(pair, CandleInterval.H1, 48)
        daily = await self._load_closes(pair, CandleInterval.D1, 40)
        return hourly, daily

    async def _load_closes(
        self, pair: str, interval: CandleInterval, limit: int
    ) -> list[tuple[datetime, float, str]]:
        """查询 K 线收盘序列（失败返回空列表，不阻断构建）。"""
        compact = to_compact(pair)
        try:
            async with get_db_session_ctx() as session:
                rows = (
                    await session.execute(
                        select(Candle)
                        .where(Candle.symbol == compact, Candle.interval == interval)
                        .order_by(Candle.observation_time.desc())
                        .limit(limit)
                    )
                ).scalars().all()
        except Exception as e:  # noqa: BLE001 - DB 不可用降级
            logger.warning(f"AlertContext: K 线加载失败 ({compact} {interval.value}): {e}")
            return []
        return [
            (
                row.observation_time,
                float(row.close),
                row.quality_status.value
                if hasattr(row.quality_status, "value") else str(row.quality_status),
            )
            for row in reversed(rows)
        ]

    # ---- 指标 ----

    async def _load_indicators(
        self, ctx: AlertContext, metrics: set[str], symbol: str
    ) -> None:
        """加载指标最新值 + 近 20 条历史（交叉检测）+ 分位。

        单个指标缺失 -> stale_fields（UNKNOWN 语义）；查询异常 -> 全部标记。
        """
        try:
            async with get_db_session_ctx() as session:
                for code in sorted(metrics):
                    rows = (
                        await session.execute(
                            select(IndicatorValue)
                            .join(
                                IndicatorDefinition,
                                IndicatorValue.indicator_id == IndicatorDefinition.id,
                            )
                            .where(
                                IndicatorDefinition.code == code,
                                IndicatorValue.symbol == symbol,
                            )
                            .order_by(IndicatorValue.observation_time.desc())
                            .limit(20)
                        )
                    ).scalars().all()
                    if not rows:
                        ctx.stale_fields.add(code)
                        continue
                    latest = rows[0]
                    ctx.indicators[code] = float(latest.value)
                    if latest.percentile is not None:
                        ctx.indicator_percentiles[code] = float(latest.percentile)
                    ctx.indicator_history[code] = [
                        (row.observation_time, float(row.value))
                        for row in reversed(rows)
                    ]
                    ctx.data_quality[code] = (
                        latest.quality_status.value
                        if hasattr(latest.quality_status, "value")
                        else str(latest.quality_status)
                    )
                    self._mark_stale_if_old(
                        ctx, code, latest.observation_time, self.INDICATOR_MAX_AGE
                    )
        except Exception as e:  # noqa: BLE001 - 指标加载失败整体降级
            logger.warning(f"AlertContext: 指标加载失败，全部标记 stale: {e}")
            ctx.stale_fields.update(metrics)

    # ---- 引擎状态 ----

    #: 引擎名 -> (ORM 模型, 状态字段)
    _ENGINE_SOURCES: dict[str, tuple[type, str]] = {
        "cycle": (CycleState, "phase"),
        "valuation": (ValuationState, "valuation_level"),
        "risk": (RiskScore, "risk_level"),
        "regime": (MarketRegime, "overall_regime"),
    }

    async def _load_engine_states(self, ctx: AlertContext, engines: set[str]) -> None:
        """加载引擎最新两行状态（当前 + 上一状态，供 state_change 条件）。"""
        for engine in sorted(engines):
            spec = self._ENGINE_SOURCES.get(engine)
            if spec is None:
                logger.debug(f"AlertContext: 未知引擎 '{engine}'，跳过")
                continue
            model, state_field = spec
            field_key = f"engine.{engine}"
            try:
                async with get_db_session_ctx() as session:
                    rows = (
                        await session.execute(
                            select(model)
                            .order_by(model.observation_time.desc())
                            .limit(2)
                        )
                    ).scalars().all()
                if not rows:
                    ctx.stale_fields.add(field_key)
                    continue
                cur = getattr(rows[0], state_field)
                ctx.engine_states[engine] = (
                    cur.value if hasattr(cur, "value") else str(cur)
                )
                if len(rows) > 1:
                    prev = getattr(rows[1], state_field)
                    ctx.engine_prev_states[engine] = (
                        prev.value if hasattr(prev, "value") else str(prev)
                    )
                quality = getattr(rows[0], "quality_status", None)
                if quality is not None:
                    ctx.data_quality[field_key] = (
                        quality.value if hasattr(quality, "value") else str(quality)
                    )
                self._mark_stale_if_old(
                    ctx, field_key, rows[0].observation_time, self.ENGINE_MAX_AGE
                )
            except Exception as e:  # noqa: BLE001 - 引擎状态加载失败降级
                logger.warning(f"AlertContext: 引擎 '{engine}' 状态加载失败: {e}")
                ctx.stale_fields.add(field_key)

    # ---- 持仓 ----

    async def _load_portfolio(self, ctx: AlertContext, user_id: Any) -> None:
        """加载用户最新组合快照（通用列遍历，容忍 schema 演进）。"""
        try:
            async with get_db_session_ctx() as session:
                row = (
                    await session.execute(
                        select(PortfolioSnapshot)
                        .where(PortfolioSnapshot.user_id == user_id)
                        .order_by(PortfolioSnapshot.observation_time.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
        except Exception as e:  # noqa: BLE001 - 持仓加载失败不阻断
            logger.warning(f"AlertContext: 持仓快照加载失败: {e}")
            return
        if row is None:
            return
        data: dict[str, Any] = {}
        for col in PortfolioSnapshot.__table__.columns:
            if col.name in {"id", "user_id"}:
                continue
            val = getattr(row, col.name, None)
            if val is None:
                continue
            if isinstance(val, Decimal):
                data[col.name] = float(val)
            elif isinstance(val, datetime):
                data[col.name] = val.isoformat()
            else:
                data[col.name] = val
        ctx.portfolio = data

    # ---- 过期标记 ----

    @staticmethod
    def _mark_stale_if_old(
        ctx: AlertContext,
        field_name: str,
        last_time: datetime | None,
        max_age_seconds: float,
    ) -> None:
        """数据过期标记：缺失或超期 -> stale_fields。"""
        if last_time is None:
            ctx.stale_fields.add(field_name)
            return
        last = last_time if last_time.tzinfo else last_time.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - last).total_seconds()
        if age > max_age_seconds:
            ctx.stale_fields.add(field_name)
