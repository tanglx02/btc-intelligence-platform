"""智能监测预警引擎（AlertEngine）。

每轮扫描流程（run_scan_once，可由后台任务或外部 scheduler 触发）：
1. 查询 is_enabled=True AND state=ACTIVE 的规则；
2. ContextBuilder 批量构建求值上下文（一次扫描只取一次数据）；
3. 逐规则三态求值（evaluator）+ 持续时间/连续次数增强检查；
4. 条件满足 + 恢复/冷却/限流闸门通过 -> 创建 AlertEvent 并尝试投递；
5. 已触发规则的恢复检查（条件不再满足 -> status=RESOLVED）；
6. 返回扫描报告 {rules_evaluated, triggered, recovered, errors, ...}。

健壮性约定：
- DB 不可用：记录日志、跳过本轮扫描，不抛出（循环持续运行）；
- Redis 不可用：DurationTracker / CooldownManager 自动降级内存实现；
- dispatcher 未实现（ImportError）：仅记录事件，不影响触发主流程。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy import select, update

from app.alerts.conditions.duration import DurationTracker
from app.alerts.context_builder import AlertContext, ContextBuilder, quality_rank
from app.alerts.cooldown import CooldownManager
from app.alerts.evaluator import AlertEvaluator
from app.alerts.schemas import ConditionNode, EvalResult
from app.core.config import settings
from app.core.database import get_db_session_ctx
from app.models.alert import AlertEvent, AlertRule
from app.models.enums import AlertRuleState, AlertStatus


class AlertEngine:
    """智能监测预警引擎。"""

    def __init__(
        self,
        context_builder: ContextBuilder | None = None,
        evaluator: AlertEvaluator | None = None,
        cooldown: CooldownManager | None = None,
        duration_tracker: DurationTracker | None = None,
    ) -> None:
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._evaluator = evaluator or AlertEvaluator()
        self._cooldown = cooldown or CooldownManager()
        self._duration = duration_tracker or DurationTracker()
        self._context_builder = context_builder or ContextBuilder()
        self._last_report: dict[str, Any] = {}
        self._last_context: AlertContext | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动后台扫描（asyncio.create_task）。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._scan_loop(), name="alert-engine-scan")
        logger.info(f"AlertEngine 已启动（扫描间隔 {self.scan_interval}s）")

    async def stop(self) -> None:
        """优雅停止。"""
        self._running = False
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.info("AlertEngine 已停止")

    @property
    def running(self) -> bool:
        """引擎是否运行中。"""
        return self._running

    @property
    def scan_interval(self) -> int:
        """扫描间隔（秒）；settings.alert_scan_interval_seconds 可覆盖，默认 60。"""
        return int(getattr(settings, "alert_scan_interval_seconds", 60) or 60)

    @property
    def last_report(self) -> dict[str, Any]:
        """最近一次扫描报告。"""
        return dict(self._last_report)

    async def _scan_loop(self) -> None:
        """后台扫描循环。"""
        while self._running:
            try:
                await self.run_scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Alert scan failed")
            await asyncio.sleep(self.scan_interval)

    # ------------------------------------------------------------------
    # 单次扫描
    # ------------------------------------------------------------------

    async def run_scan_once(self) -> dict[str, Any]:
        """单次扫描（可被外部 scheduler 调用）。"""
        report: dict[str, Any] = {
            "started_at": datetime.now(UTC).isoformat(),
            "rules_evaluated": 0,
            "triggered": 0,
            "recovered": 0,
            "suppressed": 0,
            "unknown": 0,
            "errors": 0,
        }

        # 1. 查询启用中的规则（DB 不可用 -> 跳过本轮，不崩溃）
        rules = await self._load_active_rules()
        if rules is None:
            report["errors"] += 1
            report["db_available"] = False
            self._last_report = report
            return report
        if not rules:
            self._last_report = report
            return report

        # 2. 批量构建上下文（关键性能：一次扫描只获取一次数据）
        required_metrics = self._context_builder.extract_required_metrics(rules)
        engines = self._collect_engines(rules)
        user_ids = {rule.user_id for rule in rules if rule.user_id is not None}
        try:
            context = await self._context_builder.build(
                required_metrics,
                engines=engines,
                user_id=next(iter(user_ids)) if len(user_ids) == 1 else None,
            )
        except Exception:
            logger.exception("Alert scan: 上下文构建失败，跳过本轮")
            report["errors"] += 1
            self._last_report = report
            return report
        self._last_context = context

        # 3. 已触发未恢复事件（rule_id -> event），供恢复检查
        active_events = await self._load_active_events([rule.id for rule in rules])

        # 4. 逐规则求值（priority 升序；失败互不影响）
        evaluated_ids: list[UUID] = []
        for rule in sorted(rules, key=lambda r: (r.priority, r.rule_name)):
            try:
                await self._evaluate_rule(rule, context, active_events, report)
                evaluated_ids.append(rule.id)
            except Exception:
                report["errors"] += 1
                logger.exception(f"Alert scan: 规则 '{rule.rule_name}' 求值失败")

        # 5. 批量更新 last_evaluated_at
        await self._touch_evaluated(evaluated_ids)

        report["finished_at"] = datetime.now(UTC).isoformat()
        self._last_report = report
        logger.info(
            f"Alert scan 完成: rules={report['rules_evaluated']} "
            f"triggered={report['triggered']} recovered={report['recovered']} "
            f"suppressed={report['suppressed']} unknown={report['unknown']} "
            f"errors={report['errors']}"
        )
        return report

    # ------------------------------------------------------------------
    # 单规则求值
    # ------------------------------------------------------------------

    async def _evaluate_rule(
        self,
        rule: AlertRule,
        context: AlertContext,
        active_events: dict[UUID, AlertEvent],
        report: dict[str, Any],
    ) -> None:
        """求值单条规则并按结果触发/恢复。"""
        report["rules_evaluated"] += 1

        # 数据质量门槛（低于 min_data_quality 视同 UNKNOWN，不触发不恢复）
        if not self._check_data_quality(rule, context) or not self._check_multi_source(
            rule, context
        ):
            report["unknown"] += 1
            return

        eval_result = self._evaluator.evaluate(rule.condition_tree, context)

        if eval_result.is_unknown:
            # UNKNOWN：数据缺失，保持现状（不触发、不恢复、不重置计数）
            report["unknown"] += 1
            return

        if eval_result.is_false:
            # 条件不满足：重置持续/连续状态 + 恢复检查
            await self._duration.check_duration(rule.id, False, rule.duration_seconds)
            await self._duration.check_consecutive(rule.id, False, rule.consecutive_count)
            event = active_events.get(rule.id)
            if event is not None:
                await self._handle_recovery(rule, event, context)
                report["recovered"] += 1
            return

        # 条件满足 -> 持续时间 / 连续次数增强检查（AND 语义）
        met = await self._duration.check_duration(
            rule.id, True, rule.duration_seconds
        )
        if met:
            met = await self._duration.check_consecutive(
                rule.id, True, rule.consecutive_count
            )
        if not met:
            logger.debug(
                f"Alert: 规则 '{rule.rule_name}' 条件满足但未达持续/连续要求"
            )
            return

        # 触发闸门：恢复/冷却检查 + 用户级速率限制
        ok, reason = await self._cooldown.can_trigger(rule)
        if ok:
            ok = await self._cooldown.check_rate_limit(rule.user_id)
            reason = None if ok else "RATE_LIMIT"
        if not ok:
            report["suppressed"] += 1
            logger.debug(f"Alert: 规则 '{rule.rule_name}' 被抑制: {reason}")
            return

        await self._handle_trigger(rule, eval_result, context)
        report["triggered"] += 1

    # ------------------------------------------------------------------
    # 触发 / 恢复处理
    # ------------------------------------------------------------------

    async def _handle_trigger(
        self, rule: AlertRule, eval_result: EvalResult, context: AlertContext
    ) -> None:
        """创建 AlertEvent（含完整证据链与市场快照）并更新规则状态。"""
        now = datetime.now(UTC)
        trigger_value, trigger_threshold = self._extract_trigger_values(eval_result)

        event = AlertEvent(
            rule_id=rule.id,
            user_id=rule.user_id,
            triggered_at=now,
            severity=rule.severity,
            status=AlertStatus.TRIGGERED,
            trigger_value=trigger_value,
            trigger_threshold=trigger_threshold,
            condition_result=eval_result.condition_result,
            evidence=eval_result.evidence,
            market_context=context.to_market_context(),
            data_quality=context.quality_of("price"),
            data_source=context.data_sources.get("price", ""),
            title=f"[{rule.severity.value}] {rule.rule_name}",
            description_cn=self._build_description(rule, eval_result, context),
        )

        # 持久化事件 + 更新规则计数（同一事务）
        try:
            async with get_db_session_ctx() as session:
                session.add(event)
                await session.flush()
                rule_row = await session.get(AlertRule, rule.id)
                if rule_row is not None:
                    rule_row.last_triggered_at = now
                    rule_row.trigger_count = (rule_row.trigger_count or 0) + 1
                    rule_row.consecutive_triggers = (
                        (rule_row.consecutive_triggers or 0) + 1
                    )
        except Exception:
            logger.exception(f"Alert: 事件持久化失败（规则 '{rule.rule_name}'）")
            return

        # 运行时状态：进入等恢复（冷却窗口随 last_triggered_at 生效）
        await self._cooldown.mark_triggered(rule.id, now)
        logger.warning(
            f"ALERT TRIGGERED [{rule.severity.value}] {rule.rule_name}: "
            f"{eval_result.first_true_description() or rule.condition_text or ''}"
        )

        # 投递（dispatcher 为可选插件，未实现时仅记录事件）
        await self._try_dispatch(event)

    async def _handle_recovery(
        self, rule: AlertRule, event: AlertEvent, context: AlertContext
    ) -> None:
        """条件恢复：事件状态 -> RESOLVED，规则等恢复状态清零。"""
        now = datetime.now(UTC)
        try:
            async with get_db_session_ctx() as session:
                ev = await session.get(AlertEvent, event.id)
                if ev is not None and ev.status in (
                    AlertStatus.TRIGGERED,
                    AlertStatus.ACKED,
                ):
                    ev.status = AlertStatus.RESOLVED
                    ev.resolved_at = now
                rule_row = await session.get(AlertRule, rule.id)
                if rule_row is not None:
                    rule_row.consecutive_triggers = 0
        except Exception:
            logger.exception(f"Alert: 恢复处理失败（规则 '{rule.rule_name}'）")
            return

        await self._cooldown.mark_recovered(rule.id, now)
        logger.info(f"ALERT RECOVERED: 规则 '{rule.rule_name}' 条件已恢复")

    async def _try_dispatch(self, event: AlertEvent) -> None:
        """尝试投递事件（dispatcher 未实现则跳过，仅保留事件记录）。"""
        try:
            from app.alerts.dispatcher import dispatch_event  # type: ignore[import-not-found]
        except ImportError:
            return
        try:
            await dispatch_event(event)
        except Exception:
            logger.exception(f"Alert: 事件 {event.id} 投递失败")

    # ------------------------------------------------------------------
    # 数据门槛检查
    # ------------------------------------------------------------------

    def _check_data_quality(self, rule: AlertRule, context: AlertContext) -> bool:
        """规则引用字段的数据质量是否达到 min_data_quality 门槛。

        仅检查上下文中已记录质量的字段；完全缺失的字段由求值器按
        UNKNOWN 处理（此处不拦截）。
        """
        threshold = quality_rank(rule.min_data_quality)
        try:
            node = ConditionNode.model_validate(rule.condition_tree)
        except Exception:
            return True  # 非法条件树交给求值器报错
        for metric in node.collect_metrics():
            q = context.quality_of(metric)
            if q is not None and quality_rank(q) < threshold:
                return False
        for engine in node.collect_engines():
            q = context.quality_of(f"engine.{engine}")
            if q is not None and quality_rank(q) < threshold:
                return False
        return True

    def _check_multi_source(self, rule: AlertRule, context: AlertContext) -> bool:
        """require_multi_source：需多源交叉验证通过（≥2 源）。"""
        if not rule.require_multi_source:
            return True
        return int(context.metadata.get("price_sources", 1) or 1) >= 2

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    async def _load_active_rules(self) -> list[AlertRule] | None:
        """查询启用中的规则；DB 失败返回 None（调用方跳过本轮）。"""
        try:
            async with get_db_session_ctx() as session:
                rows = (
                    await session.execute(
                        select(AlertRule).where(
                            AlertRule.is_enabled == True,  # noqa: E712
                            AlertRule.state == AlertRuleState.ACTIVE,
                        )
                    )
                ).scalars().all()
            return list(rows)
        except Exception as e:  # noqa: BLE001 - DB 不可用不崩溃
            logger.error(f"Alert scan: 规则查询失败（DB 不可用）: {e}")
            return None

    async def _load_active_events(
        self, rule_ids: list[UUID]
    ) -> dict[UUID, AlertEvent]:
        """查询活跃事件（TRIGGERED/ACKED），供恢复检查。"""
        if not rule_ids:
            return {}
        try:
            async with get_db_session_ctx() as session:
                rows = (
                    await session.execute(
                        select(AlertEvent).where(
                            AlertEvent.rule_id.in_(rule_ids),
                            AlertEvent.status.in_(
                                [AlertStatus.TRIGGERED, AlertStatus.ACKED]
                            ),
                        )
                    )
                ).scalars().all()
            return {row.rule_id: row for row in rows}
        except Exception as e:  # noqa: BLE001 - 恢复检查降级为空
            logger.warning(f"Alert scan: 活跃事件查询失败（本轮跳过恢复检查）: {e}")
            return {}

    async def _touch_evaluated(self, rule_ids: list[UUID]) -> None:
        """批量更新 last_evaluated_at（失败不影响扫描结果）。"""
        if not rule_ids:
            return
        try:
            async with get_db_session_ctx() as session:
                await session.execute(
                    update(AlertRule)
                    .where(AlertRule.id.in_(rule_ids))
                    .values(last_evaluated_at=datetime.now(UTC))
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Alert scan: 更新 last_evaluated_at 失败: {e}")

    @staticmethod
    def _collect_engines(rules: list[AlertRule]) -> set[str]:
        """收集全部规则引用的引擎名（state_change 条件）。"""
        engines: set[str] = set()
        for rule in rules:
            tree = getattr(rule, "condition_tree", None)
            if not tree:
                continue
            try:
                engines |= ConditionNode.model_validate(tree).collect_engines()
            except Exception:  # noqa: BLE001 - 非法规则跳过
                continue
        return engines

    @staticmethod
    def _extract_trigger_values(
        eval_result: EvalResult,
    ) -> tuple[Decimal | None, Decimal | None]:
        """从证据链提取首条 TRUE 的 (触发值, 阈值)。"""
        for ev in eval_result.evidence:
            if ev.get("result") != "TRUE":
                continue
            value = ev.get("value")
            threshold = ev.get("threshold")
            if value is None and threshold is None:
                continue
            return _to_decimal(value), _to_decimal(threshold)
        return None, None

    @staticmethod
    def _build_description(
        rule: AlertRule, eval_result: EvalResult, context: AlertContext
    ) -> str:
        """构建事件中文描述（规则说明 + 触发条件 + 市场快照摘要）。"""
        parts: list[str] = [rule.description or rule.condition_text or rule.rule_name]
        desc = eval_result.first_true_description()
        if desc:
            parts.append(f"触发条件：{desc}")
        price = context.current_price
        if price is not None:
            parts.append(f"当前价格 {price:,.2f}")
        changes = context.price_changes.get(context.pair_symbol, {})
        if "24h" in changes:
            parts.append(f"24h 涨跌幅 {changes['24h']:+.2f}%")
        if context.engine_states:
            states = ", ".join(
                f"{k}={v}" for k, v in sorted(context.engine_states.items())
            )
            parts.append(f"引擎状态：{states}")
        return "；".join(parts)


def _to_decimal(value: Any) -> Decimal | None:
    """安全转 Decimal（失败返回 None）。"""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


_engine: AlertEngine | None = None


def get_alert_engine() -> AlertEngine:
    """获取全局 AlertEngine 单例。"""
    global _engine
    if _engine is None:
        _engine = AlertEngine()
    return _engine


def set_alert_engine(engine: AlertEngine | None) -> None:
    """设置/重置全局 AlertEngine 单例（主要用于测试）。"""
    global _engine
    _engine = engine
