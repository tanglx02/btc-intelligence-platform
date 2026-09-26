"""通知投递编排（AlertDispatcher）。

流程（:meth:`AlertDispatcher.dispatch`）：
1. 为规则的每个通知渠道创建 :class:`~app.models.alert.AlertDelivery`
   记录（status=PENDING）；
2. Jinja2 渲染触发邮件（HTML + 纯文本，payload_snapshot 存档供审计/重试）；
3. 调用渠道 ``send()``；失败按指数退避重试（1s/4s/16s，最多 3 次重试）；
4. 成功 -> SENT + sent_at；最终失败 -> FAILED + last_error（事件不丢失，
   可由 :meth:`AlertDispatcher.retry_failed_deliveries` 定时重试）；
5. SMTP 连续 5 次发送失败 -> 标记服务异常（check_smtp_health）。

对外入口：
- :func:`dispatch_event` —— AlertEngine 每次触发事件后调用（try import 可选接入）；
- :func:`alert_maintenance_task` —— 供 scheduler 定时注册（重试失败投递 +
  检查摘要发送时间）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from jinja2 import Environment, FileSystemLoader
from loguru import logger
from sqlalchemy import select

from app.alerts.channels.base import SendResult
from app.alerts.channels.registry import ChannelRegistry
from app.alerts.schemas import ConditionNode
from app.core.config import settings
from app.core.database import get_db_session_ctx
from app.models.alert import AlertChannelConfig, AlertDelivery, AlertEvent, AlertRule

#: Jinja2 模板目录
TEMPLATE_DIR = Path(__file__).parent / "templates"

#: 触发邮件渲染后的投递内容快照键
_SNAPSHOT_KEYS = ("subject", "body_html", "body_text", "recipient")

#: 不可重试的发送错误分类（配置类错误，重试无意义）
_NO_RETRY_CATEGORIES = {"DISABLED", "INVALID_RECIPIENT", "认证失败", "收件人被拒绝"}

#: 指数退避间隔（秒）：首发失败后依次等待
_RETRY_BACKOFF = (1, 4, 16)

#: SMTP 连续失败判定阈值
_SMTP_FAIL_THRESHOLD = 5

#: 条件树指标名 -> 邮件中文标签
METRIC_LABELS: dict[str, str] = {
    "price": "价格 (USDT)",
    "drawdown_pct": "自高点回撤 (%)",
    "fear_greed": "恐惧贪婪指数",
    "mvrv": "MVRV 比率",
    "funding_rate": "资金费率",
    "open_interest": "未平仓合约量",
    "open_interest_change_24h": "持仓量 24H 变化 (%)",
    "etf_flow_1d": "ETF 净流入 (亿美元)",
    "etf_flow_7d": "ETF 7日净流入 (亿美元)",
    "risk_score": "综合风险评分",
    "rsi": "RSI",
    "portfolio_pnl_pct": "组合收益率 (%)",
    "sopr": "SOPR",
    "nupl": "NUPL",
    "puell_multiple": "Puell 倍数",
}


def _autoescape_for_template(name: str | None) -> bool:
    """按模板文件名决定是否自动转义（*.html.j2 转义，其余如 *.txt.j2 不转义）。"""
    return bool(name) and name.endswith(".html.j2")


def metric_label(metric: str) -> str:
    """指标名 -> 中文标签（未知指标原样返回）。"""
    return METRIC_LABELS.get(metric, metric)


def _fmt_metric(metric: str, value: Any) -> str:
    """按指标类型格式化数值（None -> “—”）。"""
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if metric == "price":
        return f"{v:,.2f}"
    if metric == "funding_rate":
        return f"{v * 100:.3f}%"
    if metric in {"mvrv", "sopr", "nupl", "puell_multiple"}:
        return f"{v:.2f}"
    if metric == "risk_score":
        return f"{v:.0f}"
    return f"{v:g}"


class AlertDispatcher:
    """通知投递编排。"""

    def __init__(self) -> None:
        self._registry = ChannelRegistry
        self._jinja_env: Environment | None = None
        self._smtp_fail_streak: int = 0
        self._smtp_unhealthy: bool = False

    # ------------------------------------------------------------------
    # 投递主流程
    # ------------------------------------------------------------------

    async def dispatch(
        self,
        event_id: UUID,
        rule: AlertRule | None,
        context_data: dict,
    ) -> list[dict]:
        """按规则渠道配置投递事件通知。

        Args:
            event_id: 预警事件 ID（内部加载事件详情）
            rule: 规则 ORM 对象；None 时从 DB 加载（engine 触发场景）
            context_data: 市场上下文快照（event.market_context 结构）

        Returns:
            各渠道投递结果摘要 [{channel, recipient, status, error}]
        """
        # 1. 加载事件与规则（缺任一则无法渲染）
        async with get_db_session_ctx() as session:
            event = await session.get(AlertEvent, event_id)
            if event is None:
                logger.warning(f"Dispatcher: 事件 {event_id} 不存在，跳过投递")
                return []
            if rule is None:
                rule = await session.get(AlertRule, event.rule_id)
            if rule is None:
                logger.warning(f"Dispatcher: 事件 {event_id} 的规则已删除，跳过投递")
                return []

        smtp_cfg = await self._load_smtp_config()
        event_data = self._event_data(event)
        rule_data = self._rule_data(rule)
        html, text, subject = self._render_alert_email(
            event_data, rule_data, context_data
        )

        # 2. 创建投递记录（每渠道一条，status=PENDING）
        channels = [c for c in (rule.channels or ["EMAIL"]) if c]
        recipients = {
            c: (rule.channel_config or {}).get("recipient")
            or settings.alert_default_recipient
            or smtp_cfg.get("recipient", "")
            for c in channels
        }
        delivery_ids: dict[str, UUID] = {}
        async with get_db_session_ctx() as session:
            for channel in channels:
                delivery = AlertDelivery(
                    event_id=event_id,
                    channel=channel,
                    recipient=recipients[channel] or None,
                    status="PENDING",
                    attempts=0,
                    max_attempts=1 + len(_RETRY_BACKOFF),  # 首发 + 3 次重试
                    template_used="alert_triggered",
                    payload_snapshot={
                        "subject": subject,
                        "body_html": html,
                        "body_text": text,
                        "recipient": recipients[channel],
                    },
                )
                session.add(delivery)
                await session.flush()
                delivery_ids[channel] = delivery.id

        # 3. 逐渠道发送（含指数退避重试）
        results: list[dict] = []
        for channel in channels:
            outcome = await self._deliver_with_retry(
                delivery_ids[channel], channel, recipients[channel], subject,
                html, text, smtp_cfg,
            )
            results.append(outcome)
        return results

    async def _deliver_with_retry(
        self,
        delivery_id: UUID,
        channel: str,
        recipient: str,
        subject: str,
        body_html: str,
        body_text: str,
        smtp_cfg: dict,
    ) -> dict:
        """单渠道发送 + 指数退避重试，并回写投递记录终态。"""
        channel_impl = self._registry.get(channel)
        if channel_impl is None:
            await self._finish_delivery(
                delivery_id, "FAILED",
                last_error=f"渠道 {channel} 未注册",
                response_detail={"category": "CHANNEL_NOT_REGISTERED"},
            )
            return {
                "channel": channel, "recipient": recipient,
                "status": "FAILED", "error": f"渠道 {channel} 未注册",
            }

        last_result: SendResult | None = None
        attempts = 0
        for attempt in range(1 + len(_RETRY_BACKOFF)):
            if attempt > 0:
                await asyncio.sleep(_RETRY_BACKOFF[attempt - 1])
            attempts = attempt + 1
            last_result = await channel_impl.send(
                recipient, subject, body_html, body_text, smtp_cfg
            )
            if last_result.success:
                break
            # 配置类错误不重试
            category = str((last_result.response or {}).get("category", ""))
            if category in _NO_RETRY_CATEGORIES:
                break
            if attempt < len(_RETRY_BACKOFF):
                logger.warning(
                    f"Dispatcher: 渠道 {channel} 第 {attempts} 次发送失败，"
                    f"将退避 {_RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]}s 重试: "
                    f"{last_result.error}"
                )

        self._track_smtp_health(channel, last_result)
        status = "SENT" if (last_result and last_result.success) else "FAILED"
        await self._finish_delivery(
            delivery_id, status,
            attempts=attempts,
            message_id=last_result.message_id if last_result else None,
            last_error=None if last_result and last_result.success else (
                last_result.error if last_result else "发送结果未知"
            ),
            response_detail=dict(last_result.response) if last_result else {},
        )
        return {
            "channel": channel,
            "recipient": recipient,
            "status": status,
            "error": None if status == "SENT" else (
                last_result.error if last_result else "发送结果未知"
            ),
        }

    async def _finish_delivery(
        self,
        delivery_id: UUID,
        status: str,
        *,
        attempts: int | None = None,
        message_id: str | None = None,
        last_error: str | None = None,
        response_detail: dict | None = None,
    ) -> None:
        """回写投递记录终态（SENT/FAILED）。"""
        try:
            async with get_db_session_ctx() as session:
                delivery = await session.get(AlertDelivery, delivery_id)
                if delivery is None:
                    return
                delivery.status = status
                if attempts is not None:
                    delivery.attempts = attempts
                now = datetime.now(UTC)
                if status == "SENT":
                    delivery.sent_at = now
                    delivery.response_detail = {
                        **(response_detail or {}),
                        "message_id": message_id,
                    }
                else:
                    delivery.failed_at = now
                    delivery.last_error = last_error
                    delivery.response_detail = response_detail or {}
        except Exception:  # noqa: BLE001 - 状态回写失败不阻断主流程
            logger.exception(f"Dispatcher: 投递记录 {delivery_id} 状态回写失败")

    # ------------------------------------------------------------------
    # 失败重试（定时任务入口）
    # ------------------------------------------------------------------

    async def retry_failed_deliveries(self, limit: int = 50) -> int:
        """重试所有 FAILED 且 attempts < max_attempts 的投递。

        直接复用 payload_snapshot 中存档的渲染内容，无需重新渲染。
        返回本轮成功重试的条数。
        """
        try:
            async with get_db_session_ctx() as session:
                rows = (
                    await session.execute(
                        select(AlertDelivery)
                        .where(
                            AlertDelivery.status == "FAILED",
                            AlertDelivery.attempts < AlertDelivery.max_attempts,
                        )
                        .order_by(AlertDelivery.updated_at.desc())
                        .limit(limit)
                    )
                ).scalars().all()
                pending = [
                    (row.id, row.channel, row.recipient or "", row.payload_snapshot or {})
                    for row in rows
                ]
        except Exception:  # noqa: BLE001 - DB 不可用跳过本轮
            logger.exception("Dispatcher: 失败投递查询失败（本轮跳过）")
            return 0

        smtp_cfg = await self._load_smtp_config()
        retried = 0
        for delivery_id, channel, recipient, snapshot in pending:
            channel_impl = self._registry.get(channel)
            if channel_impl is None or not recipient:
                await self._finish_delivery(
                    delivery_id, "FAILED",
                    last_error="渠道未注册或收件人为空，无法重试",
                )
                continue
            result = await channel_impl.send(
                recipient,
                snapshot.get("subject", "【BTC智能预警】"),
                snapshot.get("body_html", ""),
                snapshot.get("body_text", ""),
                smtp_cfg,
            )
            self._track_smtp_health(channel, result)
            if result.success:
                retried += 1
                await self._finish_delivery(
                    delivery_id, "SENT", message_id=result.message_id,
                    response_detail=dict(result.response),
                )
            else:
                await self._finish_delivery(
                    delivery_id, "FAILED", last_error=result.error,
                    response_detail=dict(result.response),
                )
        if pending:
            logger.info(
                f"Dispatcher: 失败投递重试完成 {retried}/{len(pending)} 成功"
            )
        return retried

    # ------------------------------------------------------------------
    # SMTP 健康检测
    # ------------------------------------------------------------------

    def _track_smtp_health(self, channel: str, result: SendResult | None) -> None:
        """连续失败计数（仅统计 EMAIL 渠道的真实发送失败）。"""
        if channel != "EMAIL":
            return
        success = bool(result and result.success)
        if success:
            if self._smtp_fail_streak > 0 or self._smtp_unhealthy:
                logger.info("Dispatcher: SMTP 通道恢复正常")
            self._smtp_fail_streak = 0
            self._smtp_unhealthy = False
            return
        disabled = (result.response or {}).get("category") == "DISABLED"
        if disabled:
            return  # 未启用不算服务故障
        self._smtp_fail_streak += 1
        if self._smtp_fail_streak >= _SMTP_FAIL_THRESHOLD and not self._smtp_unhealthy:
            self._smtp_unhealthy = True
            logger.error(
                f"Dispatcher: SMTP 连续 {self._smtp_fail_streak} 次发送失败，"
                f"标记邮件服务异常"
            )

    async def check_smtp_health(self) -> dict:
        """SMTP 连续失败检测（连续 5 次失败标记服务异常）。"""
        return {
            "healthy": not self._smtp_unhealthy,
            "consecutive_failures": self._smtp_fail_streak,
            "threshold": _SMTP_FAIL_THRESHOLD,
        }

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------

    def _get_jinja_env(self) -> Environment:
        """Jinja2 环境（HTML 模板自动转义；纯文本模板不转义）。"""
        if self._jinja_env is None:
            self._jinja_env = Environment(
                loader=FileSystemLoader(str(TEMPLATE_DIR)),
                autoescape=_autoescape_for_template,
                trim_blocks=True,
                lstrip_blocks=True,
            )
        return self._jinja_env

    def _render_alert_email(
        self,
        event_data: dict,
        rule_data: dict,
        context_data: dict,
    ) -> tuple[str, str, str]:
        """渲染触发邮件 -> (html, text)（subject 第三返回值）。"""
        env = self._get_jinja_env()
        template_vars = self._build_template_vars(event_data, rule_data, context_data)

        html = env.get_template("alert_triggered.html.j2").render(**template_vars)
        text = env.get_template("alert_triggered.txt.j2").render(**template_vars)
        severity = rule_data.get("severity") or "MEDIUM"
        subject = (
            f"【{severity}】BTC智能预警 - {rule_data.get('rule_name', '规则触发')}"
        )
        return html, text, subject

    def _build_template_vars(
        self,
        event_data: dict,
        rule_data: dict,
        context_data: dict,
    ) -> dict:
        """组装两套模板共用的变量（数值全部预格式化为字符串）。"""
        indicators: dict[str, Any] = context_data.get("indicators") or {}
        percentiles: dict[str, Any] = context_data.get("indicator_percentiles") or {}
        engine_states: dict[str, str] = context_data.get("engine_states") or {}
        data_quality: dict[str, str] = context_data.get("data_quality") or {}
        data_sources: dict[str, str] = context_data.get("data_sources") or {}
        metadata: dict[str, Any] = context_data.get("metadata") or {}

        # 相关指标表：规则条件树引用的指标 + 价格
        metric_rows: list[dict] = []
        try:
            tree = rule_data.get("condition_tree") or {}
            metrics = ConditionNode.model_validate(tree).collect_metrics()
        except Exception:  # noqa: BLE001 - 非法条件树降级为仅价格
            metrics = set()
        ordered = sorted(metrics, key=lambda m: (m != "price", m))
        for metric in ordered:
            value = (
                context_data.get("price")
                if metric == "price"
                else indicators.get(metric)
            )
            metric_rows.append({
                "label": metric_label(metric),
                "current": _fmt_metric(metric, value),
                "threshold": _fmt_metric(metric, rule_data.get("thresholds", {}).get(metric)),
                "percentile": (
                    f"{float(percentiles[metric]):.1f}%"
                    if metric in percentiles and percentiles[metric] is not None
                    else "—"
                ),
            })

        changes: dict[str, float] = (
            context_data.get("price_changes") or {}
        ).get(context_data.get("pair_symbol") or "BTC/USDT", {})
        price_change = changes.get("24h")

        market = {
            "price": _fmt_metric("price", context_data.get("price")),
            "price_change_24h": price_change,
            "cycle_phase": engine_states.get("cycle"),
            "valuation_level": engine_states.get("valuation"),
            "risk_level": engine_states.get("risk"),
            "overall_regime": engine_states.get("regime"),
        }
        failover = bool(metadata.get("failover_occurred")) or bool(
            metadata.get("failover_events")
        )
        data_info = {
            "source": data_sources.get("price") or "—",
            "quality": data_quality.get("price") or "—",
            "failover": failover,
        }

        return {
            "platform_name": settings.app_name,
            "platform_url": settings.platform_url.rstrip("/"),
            "rule": rule_data,
            "event": event_data,
            "triggered_at": event_data.get("triggered_at") or "—",
            "metrics": metric_rows,
            "market": market,
            "data_info": data_info,
        }

    # ------------------------------------------------------------------
    # 数据提取与运行时配置
    # ------------------------------------------------------------------

    @staticmethod
    def _rule_data(rule: AlertRule) -> dict:
        """提取渲染所需规则字段（含条件树阈值映射）。"""
        thresholds: dict[str, Any] = {}
        try:
            tree = ConditionNode.model_validate(rule.condition_tree)
            for node in tree.iter_nodes():
                if node.type in {"AND", "OR", "NOT"}:
                    continue
                key = node.metric or ""
                if key and node.value is not None:
                    thresholds[key] = node.value
                elif key and node.percentile is not None:
                    thresholds[key] = node.percentile
        except Exception:  # noqa: BLE001 - 阈值提取失败仅影响表格展示
            logger.debug("Dispatcher: 条件树阈值提取失败（表格阈值列将显示 —）")
        return {
            "rule_name": rule.rule_name,
            "severity": (
                rule.severity.value if hasattr(rule.severity, "value") else str(rule.severity)
            ),
            "category": rule.category,
            "condition_text": rule.condition_text or rule.description or rule.rule_name,
            "target_symbol": rule.target_symbol,
            "condition_tree": rule.condition_tree,
            "thresholds": thresholds,
        }

    @staticmethod
    def _event_data(event: AlertEvent) -> dict:
        """提取渲染所需事件字段。"""
        triggered_at = event.triggered_at
        if triggered_at is not None and triggered_at.tzinfo is None:
            triggered_at = triggered_at.replace(tzinfo=UTC)
        return {
            "title": event.title,
            "description": event.description_cn or event.title,
            "triggered_at": (
                triggered_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
                if triggered_at else "—"
            ),
            "trigger_value": float(event.trigger_value)
            if event.trigger_value is not None else None,
            "trigger_threshold": float(event.trigger_threshold)
            if event.trigger_threshold is not None else None,
        }

    @staticmethod
    async def _load_smtp_config() -> dict:
        """加载 SMTP 运行时配置（DB 系统级 AlertChannelConfig，失败回退空 dict）。

        EmailChannel 内部再与 settings.smtp_* 合并（config 键优先）。
        """
        try:
            async with get_db_session_ctx() as session:
                row = (
                    await session.execute(
                        select(AlertChannelConfig)
                        .where(
                            AlertChannelConfig.channel_type == "SMTP",
                            AlertChannelConfig.user_id.is_(None),  # type: ignore[union-attr]
                            AlertChannelConfig.is_enabled.is_(True),  # type: ignore[union-attr]
                        )
                        .order_by(AlertChannelConfig.updated_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
            return dict(row.config) if row is not None else {}
        except Exception as e:  # noqa: BLE001 - DB 不可用回退环境变量配置
            logger.warning(f"Dispatcher: SMTP 运行时配置加载失败，回退 env: {e}")
            return {}


_dispatcher: AlertDispatcher | None = None


def get_dispatcher() -> AlertDispatcher:
    """获取全局 AlertDispatcher 单例（engine 通过 try import 接入）。"""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = AlertDispatcher()
    return _dispatcher


def set_dispatcher(dispatcher: AlertDispatcher | None) -> None:
    """设置/重置全局单例（测试用）。"""
    global _dispatcher
    _dispatcher = dispatcher


async def dispatch_event(event: AlertEvent) -> None:
    """AlertEngine 投递入口（``engine._try_dispatch`` 调用）。

    engine 以 ``from app.alerts.dispatcher import dispatch_event`` 方式
    可选接入，签名必须保持 ``async def dispatch_event(event)``。
    """
    context_data = dict(event.market_context or {})
    await get_dispatcher().dispatch(event.id, None, context_data)


async def alert_maintenance_task() -> dict:
    """Alert 维护任务：重试失败投递 + 检查摘要发送时间（供 scheduler 注册）。"""
    result: dict[str, Any] = {}
    try:
        dispatcher = get_dispatcher()
        result["retried_deliveries"] = await dispatcher.retry_failed_deliveries()
        result["smtp_health"] = await dispatcher.check_smtp_health()
    except Exception:  # noqa: BLE001 - 维护任务不抛出
        logger.exception("Alert maintenance: 失败投递重试异常")

    try:
        from app.alerts.digest import get_digest_service

        result["digest"] = await get_digest_service().maybe_send_scheduled()
    except ImportError:
        result["digest"] = "digest module not available"
    except Exception:  # noqa: BLE001
        logger.exception("Alert maintenance: 摘要发送检查异常")
    return result
