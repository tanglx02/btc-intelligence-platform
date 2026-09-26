"""Alert 业务服务（规则 CRUD / 事件 / 测试 / 统计 / 历史回测）。

供 ``app.api.v1.alerts`` 路由调用。历史回测采用「每日快照式求值」：
以日线 K 线与指标历史逐日构造求值上下文，复用 :class:`AlertEvaluator`
（与实时扫描同一求值逻辑），state_change 条件因缺历史引擎输出按 UNKNOWN
处理（不触发）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy import func, select

from app.alerts.context_builder import AlertContext, ContextBuilder
from app.alerts.evaluator import AlertEvaluator
from app.alerts.schemas import AlertRuleCreate, ConditionNode
from app.core.database import get_db_session_ctx
from app.models.alert import AlertEvent, AlertRule
from app.models.enums import AlertRuleState, AlertStatus, CandleInterval
from app.models.indicator import IndicatorDefinition, IndicatorValue
from app.models.market import Candle
from app.providers.market.symbol_mapper import to_compact

QUOTE_CCY = "USDT"

#: 规则可更新字段白名单
_RULE_EDITABLE = {
    "rule_name", "description", "severity", "category", "target_symbol",
    "condition_tree", "condition_text", "duration_seconds", "consecutive_count",
    "cooldown_seconds", "channels", "channel_config", "min_data_quality",
    "require_multi_source", "priority", "tags",
}

#: 回测触发后观察窗口（天）
_BACKTEST_HORIZONS = (7, 30, 90)


def _enum_str(v: Any) -> str | None:
    """枚举/字符串 -> str（None 安全）。"""
    if v is None:
        return None
    return v.value if hasattr(v, "value") else str(v)


def _iso(v: datetime | None) -> str | None:
    if v is None:
        return None
    dt = v if v.tzinfo else v.replace(tzinfo=UTC)
    return dt.isoformat()


def rule_to_dict(rule: AlertRule) -> dict:
    """AlertRule ORM -> 响应 dict。"""
    return {
        "id": str(rule.id),
        "user_id": str(rule.user_id) if rule.user_id else None,
        "rule_name": rule.rule_name,
        "description": rule.description,
        "severity": _enum_str(rule.severity),
        "category": rule.category,
        "target_symbol": rule.target_symbol,
        "condition_tree": rule.condition_tree,
        "condition_text": rule.condition_text,
        "duration_seconds": rule.duration_seconds,
        "consecutive_count": rule.consecutive_count,
        "cooldown_seconds": rule.cooldown_seconds,
        "channels": rule.channels,
        "channel_config": rule.channel_config,
        "min_data_quality": rule.min_data_quality,
        "require_multi_source": rule.require_multi_source,
        "state": _enum_str(rule.state),
        "is_enabled": rule.is_enabled,
        "priority": rule.priority,
        "last_evaluated_at": _iso(rule.last_evaluated_at),
        "last_triggered_at": _iso(rule.last_triggered_at),
        "trigger_count": rule.trigger_count,
        "consecutive_triggers": rule.consecutive_triggers,
        "tags": rule.tags,
        "created_at": _iso(rule.created_at),
        "updated_at": _iso(rule.updated_at),
    }


def event_to_dict(event: AlertEvent, rule: AlertRule | None = None) -> dict:
    """AlertEvent ORM -> 响应 dict。"""
    data = {
        "id": str(event.id),
        "rule_id": str(event.rule_id),
        "rule_name": rule.rule_name if rule is not None else None,
        "user_id": str(event.user_id) if event.user_id else None,
        "triggered_at": _iso(event.triggered_at),
        "resolved_at": _iso(event.resolved_at),
        "severity": _enum_str(event.severity),
        "status": _enum_str(event.status),
        "trigger_value": float(event.trigger_value) if event.trigger_value is not None else None,
        "trigger_threshold": (
            float(event.trigger_threshold)
            if event.trigger_threshold is not None else None
        ),
        "condition_result": event.condition_result,
        "evidence": event.evidence,
        "market_context": event.market_context,
        "data_quality": event.data_quality,
        "data_source": event.data_source,
        "title": event.title,
        "description_cn": event.description_cn,
        "is_suppressed": event.is_suppressed,
        "suppress_reason": event.suppress_reason,
        "acked_at": _iso(event.acked_at),
        "created_at": _iso(event.created_at),
    }
    return data


class AlertService:
    """Alert 业务服务。"""

    def __init__(self) -> None:
        self._evaluator = AlertEvaluator()

    # ------------------------------------------------------------------
    # 规则 CRUD
    # ------------------------------------------------------------------

    async def create_rule(
        self, data: AlertRuleCreate, user_id: UUID | None = None
    ) -> dict:
        """创建规则（AlertRuleCreate 已校验条件树并生成 condition_text）。"""
        from app.models.enums import AlertSeverity

        rule = AlertRule(
            user_id=user_id,
            rule_name=data.rule_name,
            description=data.description or None,
            severity=AlertSeverity(data.severity),
            category=data.category,
            target_symbol=data.target_symbol,
            condition_tree=data.condition_tree.model_dump(),
            condition_text=data.condition_text,
            duration_seconds=data.duration_seconds,
            consecutive_count=data.consecutive_count,
            cooldown_seconds=data.cooldown_seconds,
            channels=data.channels,
            channel_config=data.channel_config,
            min_data_quality=data.min_data_quality,
            require_multi_source=data.require_multi_source,
            priority=data.priority,
            tags=data.tags,
        )
        async with get_db_session_ctx() as session:
            session.add(rule)
            await session.flush()
            await session.refresh(rule)
            return rule_to_dict(rule)

    async def list_rules(
        self,
        status: str | None = None,
        severity: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> dict:
        """规则列表（分页 + 状态/严重程度过滤）。"""
        page, size = max(page, 1), min(max(size, 1), 100)
        async with get_db_session_ctx() as session:
            query = select(AlertRule).order_by(
                AlertRule.priority.asc(), AlertRule.rule_name.asc()
            )
            count_query = select(func.count()).select_from(AlertRule)
            if status:
                query = query.where(AlertRule.state == AlertRuleState(status))
                count_query = count_query.where(AlertRule.state == AlertRuleState(status))
            if severity:
                from app.models.enums import AlertSeverity

                query = query.where(AlertRule.severity == AlertSeverity(severity))
                count_query = count_query.where(
                    AlertRule.severity == AlertSeverity(severity)
                )
            total = (await session.execute(count_query)).scalar() or 0
            rows = (
                await session.execute(query.offset((page - 1) * size).limit(size))
            ).scalars().all()
        return {"items": [rule_to_dict(r) for r in rows], "total": int(total),
                "page": page, "size": size}

    async def get_rule(self, rule_id: UUID) -> dict | None:
        """规则详情（不存在返回 None）。"""
        async with get_db_session_ctx() as session:
            rule = await session.get(AlertRule, rule_id)
        return rule_to_dict(rule) if rule is not None else None

    async def update_rule(self, rule_id: UUID, updates: dict) -> dict | None:
        """部分更新规则（condition_tree 更新时重新校验并刷新 condition_text）。"""
        async with get_db_session_ctx() as session:
            rule = await session.get(AlertRule, rule_id)
            if rule is None:
                return None
            for key, value in updates.items():
                if key not in _RULE_EDITABLE:
                    continue
                if key == "condition_tree":
                    # 校验并刷新人类可读描述
                    node = ConditionNode.model_validate(value)
                    node.validate_tree()
                    rule.condition_tree = node.model_dump()
                    rule.condition_text = node.to_human_text()
                elif key in ("severity",):
                    from app.models.enums import AlertSeverity

                    setattr(rule, key, AlertSeverity(value))
                elif key == "state":
                    setattr(rule, key, AlertRuleState(value))
                else:
                    setattr(rule, key, value)
            await session.flush()
            await session.refresh(rule)
            return rule_to_dict(rule)

    async def delete_rule(self, rule_id: UUID) -> bool:
        """软删规则（is_enabled=False + state=ARCHIVED，保留事件历史）。"""
        async with get_db_session_ctx() as session:
            rule = await session.get(AlertRule, rule_id)
            if rule is None:
                return False
            rule.is_enabled = False
            rule.state = AlertRuleState.ARCHIVED
        return True

    async def pause_rule(self, rule_id: UUID) -> dict | None:
        """暂停规则（state=PAUSED）。"""
        return await self.update_rule(rule_id, {"state": "PAUSED", "is_enabled": False})

    async def resume_rule(self, rule_id: UUID) -> dict | None:
        """恢复规则（state=ACTIVE）。"""
        return await self.update_rule(rule_id, {"state": "ACTIVE", "is_enabled": True})

    # ------------------------------------------------------------------
    # 测试 / 事件 / 统计
    # ------------------------------------------------------------------

    async def test_rule(self, rule_id: UUID) -> dict:
        """立即求值一次（ContextBuilder.build + evaluator），返回命中与证据。"""
        async with get_db_session_ctx() as session:
            rule = await session.get(AlertRule, rule_id)
        if rule is None:
            return {"found": False}
        node = ConditionNode.model_validate(rule.condition_tree)
        builder = ContextBuilder()
        context = await builder.build(
            node.collect_metrics(),
            engines=node.collect_engines(),
            user_id=rule.user_id,
        )
        result = self._evaluator.evaluate(rule.condition_tree, context)
        return {
            "found": True,
            "rule_id": str(rule.id),
            "rule_name": rule.rule_name,
            "triggered": result.triggered,
            "result": result.result,
            "evidence": result.evidence,
            "condition_result": result.condition_result,
            "context_snapshot": {
                "price": context.current_price,
                "indicators": dict(context.indicators),
                "stale_fields": sorted(context.stale_fields),
                "engine_states": dict(context.engine_states),
                "data_quality": dict(context.data_quality),
            },
        }

    async def get_events(
        self,
        rule_id: UUID | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        page: int = 1,
        size: int = 20,
    ) -> dict:
        """触发历史（分页 + 规则/时间过滤）。"""
        page, size = max(page, 1), min(max(size, 1), 100)
        async with get_db_session_ctx() as session:
            query = (
                select(AlertEvent, AlertRule)
                .outerjoin(AlertRule, AlertEvent.rule_id == AlertRule.id)
                .order_by(AlertEvent.triggered_at.desc())
            )
            count_query = select(func.count()).select_from(AlertEvent)
            conds = []
            if rule_id is not None:
                conds.append(AlertEvent.rule_id == rule_id)
            if start is not None:
                conds.append(AlertEvent.triggered_at >= start)
            if end is not None:
                conds.append(AlertEvent.triggered_at <= end)
            for cond in conds:
                query = query.where(cond)
                count_query = count_query.where(cond)
            total = (await session.execute(count_query)).scalar() or 0
            rows = (
                await session.execute(query.offset((page - 1) * size).limit(size))
            ).all()
        items = [event_to_dict(event, rule) for event, rule in rows]
        return {"items": items, "total": int(total), "page": page, "size": size}

    async def ack_event(self, event_id: UUID) -> dict | None:
        """确认事件（status=ACKED）。"""
        async with get_db_session_ctx() as session:
            event = await session.get(AlertEvent, event_id)
            if event is None:
                return None
            if event.status == AlertStatus.TRIGGERED:
                event.status = AlertStatus.ACKED
                event.acked_at = datetime.now(UTC)
            await session.refresh(event)
            return event_to_dict(event)

    async def get_stats(self) -> dict:
        """统计：总规则数 / 活跃数 / 今日触发 / 各 severity 分布。"""
        today_start = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        async with get_db_session_ctx() as session:
            total_rules = (
                await session.execute(
                    select(func.count()).select_from(AlertRule)
                    .where(AlertRule.is_enabled == True)  # noqa: E712
                )
            ).scalar() or 0
            active_rules = (
                await session.execute(
                    select(func.count()).select_from(AlertRule)
                    .where(
                        AlertRule.is_enabled == True,  # noqa: E712
                        AlertRule.state == AlertRuleState.ACTIVE,
                    )
                )
            ).scalar() or 0
            paused_rules = (
                await session.execute(
                    select(func.count()).select_from(AlertRule)
                    .where(AlertRule.state == AlertRuleState.PAUSED)
                )
            ).scalar() or 0
            today_triggered = (
                await session.execute(
                    select(func.count()).select_from(AlertEvent)
                    .where(AlertEvent.triggered_at >= today_start)
                )
            ).scalar() or 0
            unacked_events = (
                await session.execute(
                    select(func.count()).select_from(AlertEvent)
                    .where(AlertEvent.status == AlertStatus.TRIGGERED)
                )
            ).scalar() or 0
            severity_rows = (
                await session.execute(
                    select(AlertRule.severity, func.count())
                    .where(AlertRule.is_enabled == True)  # noqa: E712
                    .group_by(AlertRule.severity)
                )
            ).all()
        return {
            "total_rules": int(total_rules),
            "active_rules": int(active_rules),
            "paused_rules": int(paused_rules),
            "today_triggered": int(today_triggered),
            "unacked_events": int(unacked_events),
            "severity_distribution": {
                _enum_str(sev) or "UNKNOWN": int(cnt) for sev, cnt in severity_rows
            },
        }

    # ------------------------------------------------------------------
    # 历史触发模拟（回测）
    # ------------------------------------------------------------------

    async def backtest_rule(
        self, rule_id: UUID, start: datetime, end: datetime
    ) -> dict:
        """历史触发模拟：用历史数据逐日求值规则在过去何时会触发。

        Returns:
            {trigger_dates, trigger_count, avg_interval_days,
             performance: {"7d": [...], "30d": [...], "90d": [...]}, note}
        """
        start = start.replace(tzinfo=start.tzinfo or UTC)
        end = end.replace(tzinfo=end.tzinfo or UTC)
        if end <= start:
            end = start + timedelta(days=30)

        async with get_db_session_ctx() as session:
            rule = await session.get(AlertRule, rule_id)
        if rule is None:
            return {"found": False}
        node = ConditionNode.model_validate(rule.condition_tree)
        metrics = node.collect_metrics()
        has_state_change = bool(node.collect_engines())

        # 历史数据（多取 40 天供首个求值日的变化率计算）
        hist_start = start - timedelta(days=40)
        closes = await self._load_daily_closes(hist_start, end)
        indicator_series = await self._load_indicator_history(metrics, hist_start, end)

        trigger_dates: list[datetime] = []
        in_condition = False  # 连续 TRUE 视为一次触发（模拟恢复机制）
        evaluator = AlertEvaluator()

        for i, (ts, close) in enumerate(closes):
            if ts < start:
                continue
            ctx = self._build_day_context(closes, i, indicator_series, ts)
            result = evaluator.evaluate(rule.condition_tree, ctx)
            if result.is_true and not in_condition:
                trigger_dates.append(ts)
                in_condition = True
            elif result.is_false:
                in_condition = False

        performance = self._trigger_performance(closes, trigger_dates)
        intervals = [
            (trigger_dates[i] - trigger_dates[i - 1]).days
            for i in range(1, len(trigger_dates))
        ]
        notes: list[str] = []
        if has_state_change:
            notes.append("state_change 条件不支持历史回测（按 UNKNOWN 处理）")
        if not closes:
            notes.append("历史 K 线数据不足，无法回测")

        return {
            "found": True,
            "rule_id": str(rule.id),
            "rule_name": rule.rule_name,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "trigger_dates": [t.strftime("%Y-%m-%d") for t in trigger_dates],
            "trigger_count": len(trigger_dates),
            "avg_interval_days": (
                round(sum(intervals) / len(intervals), 1) if intervals else None
            ),
            "performance": performance,
            "note": "；".join(notes) or None,
        }

    def _build_day_context(
        self,
        closes: list[tuple[datetime, float]],
        index: int,
        indicator_series: dict[str, list[tuple[datetime, float, float | None]]],
        day: datetime,
    ) -> AlertContext:
        """构造单日历史求值上下文。"""
        pair = f"BTC/{QUOTE_CCY}"
        ctx = AlertContext(timestamp=day, symbol="BTC", pair_symbol=pair)
        ctx.prices[pair] = closes[index][1]
        changes: dict[str, float] = {}
        for label, back in (("24h", 1), ("7d", 7)):
            j = index - back
            if j >= 0 and closes[j][1]:
                changes[label] = round(
                    (closes[index][1] - closes[j][1]) / closes[j][1] * 100, 4
                )
        ctx.price_changes[pair] = changes

        for code, series in indicator_series.items():
            history = [(t, v) for t, v, _p in series if t <= day]
            if not history:
                continue
            ctx.indicator_history[code] = history[-20:]
            value, percentile = history[-1][1], None
            for t, v, p in series:
                if t == history[-1][0]:
                    percentile = p
                    break
            ctx.indicators[code] = value
            if percentile is not None:
                ctx.indicator_percentiles[code] = percentile
        return ctx

    async def _load_daily_closes(
        self, start: datetime, end: datetime
    ) -> list[tuple[datetime, float]]:
        """日线收盘序列（升序；失败返回空）。"""
        pair = to_compact(f"BTC/{QUOTE_CCY}")
        try:
            async with get_db_session_ctx() as session:
                rows = (
                    await session.execute(
                        select(Candle)
                        .where(
                            Candle.symbol == pair,
                            Candle.interval == CandleInterval.D1,
                            Candle.observation_time >= start,
                            Candle.observation_time <= end,
                        )
                        .order_by(Candle.observation_time.asc())
                    )
                ).scalars().all()
            return [(r.observation_time, float(r.close)) for r in rows]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"AlertService: 历史K线加载失败: {e}")
            return []

    async def _load_indicator_history(
        self, metrics: set[str], start: datetime, end: datetime
    ) -> dict[str, list[tuple[datetime, float, float | None]]]:
        """指标历史序列 code -> [(time, value, percentile)]（失败返回空）。"""
        series: dict[str, list[tuple[datetime, float, float | None]]] = {}
        if not metrics:
            return series
        try:
            async with get_db_session_ctx() as session:
                rows = (
                    await session.execute(
                        select(IndicatorValue, IndicatorDefinition.code)
                        .join(
                            IndicatorDefinition,
                            IndicatorValue.indicator_id == IndicatorDefinition.id,
                        )
                        .where(
                            IndicatorDefinition.code.in_(sorted(metrics)),
                            IndicatorValue.symbol == "BTC",
                            IndicatorValue.observation_time >= start,
                            IndicatorValue.observation_time <= end,
                        )
                        .order_by(IndicatorValue.observation_time.asc())
                    )
                ).all()
            for value_row, code in rows:
                series.setdefault(code, []).append(
                    (
                        value_row.observation_time,
                        float(value_row.value),
                        float(value_row.percentile) if value_row.percentile is not None else None,
                    )
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"AlertService: 指标历史加载失败: {e}")
        return series

    @staticmethod
    def _trigger_performance(
        closes: list[tuple[datetime, float]], trigger_dates: list[datetime]
    ) -> dict[str, list[dict]]:
        """触发后 7/30/90 天价格表现。"""
        performance: dict[str, list[dict]] = {
            str(h): [] for h in _BACKTEST_HORIZONS
        }
        if not closes:
            return performance
        for trig in trigger_dates:
            base = next(
                (c for t, c in closes if t >= trig), None
            )
            if base is None:
                continue
            for horizon in _BACKTEST_HORIZONS:
                target = trig + timedelta(days=horizon)
                future = next(
                    (c for t, c in closes if t >= target), None
                )
                if future is not None:
                    performance[str(horizon)].append({
                        "date": trig.strftime("%Y-%m-%d"),
                        "change_pct": round((future - base) / base * 100, 2),
                    })
        return performance


_alert_service: AlertService | None = None


def get_alert_service() -> AlertService:
    """获取全局 AlertService 单例。"""
    global _alert_service
    if _alert_service is None:
        _alert_service = AlertService()
    return _alert_service


def set_alert_service(service: AlertService | None) -> None:
    """设置/重置全局单例（测试用）。"""
    global _alert_service
    _alert_service = service
