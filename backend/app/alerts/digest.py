"""每日摘要 + 周报服务（DigestService）。

数据采集全部 try/except 优雅降级：单项数据失败 -> 该项为 None/空列表，
不阻断摘要发送（缺数据的板块在邮件模板中自动隐藏）。

发送配置持久化在 ``alert_channel_configs``（channel_type='DIGEST'，系统级），
config JSON 结构::

    {
      "enabled": true,          # 是否随调度自动发送
      "daily_hour": 8,          # 每日摘要发送小时（scheduler_timezone 本地时间）
      "weekly_day": 0,          # 周报发送日（0=周一）
      "weekly_hour": 9,         # 周报发送小时
      "recipients": [],         # 收件人列表（空 -> settings.alert_default_recipient）
      "last_daily_sent": "2026-09-27",   # 已发送日期（防重复）
      "last_weekly_sent": "2026-W39"
    }

供 scheduler 注册的入口：:func:`DigestService.maybe_send_scheduled`
（由 ``app.alerts.dispatcher.alert_maintenance_task`` 调用）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader
from loguru import logger
from sqlalchemy import select

from app.alerts.channels.email_smtp import EmailChannel
from app.alerts.channels.registry import ChannelRegistry
from app.core.config import settings
from app.core.database import get_db_session_ctx
from app.models.alert import AlertChannelConfig, AlertEvent, AlertRule
from app.models.engine import CycleState, MarketRegime, RiskScore, ValuationState
from app.models.enums import AlertStatus, CandleInterval
from app.models.etf import EtfFlow
from app.models.indicator import IndicatorDefinition, IndicatorValue
from app.models.market import Candle
from app.models.onchain import OnchainMetric
from app.models.portfolio import PortfolioSnapshot
from app.providers.market.symbol_mapper import to_compact

#: 模板目录（与 dispatcher 共享）
_TEMPLATE_DIR = Path(__file__).parent / "templates"

#: 每日摘要展示的关键指标（indicator_definitions.code）
_DAILY_INDICATOR_CODES: tuple[str, ...] = (
    "mvrv", "fear_greed", "rsi", "funding_rate", "sopr", "nupl",
)

#: 链上摘要指标（onchain_metrics.metric_name）
_ONCHAIN_METRICS: tuple[str, ...] = ("MVRV", "SOPR", "NUPL", "PUELL_MULTIPLE")

#: 指标 code -> 中文标签
_INDICATOR_LABELS: dict[str, str] = {
    "mvrv": "MVRV 比率",
    "fear_greed": "恐惧贪婪指数",
    "rsi": "RSI (14)",
    "funding_rate": "资金费率",
    "sopr": "SOPR",
    "nupl": "NUPL",
}

#: 引擎名 -> 中文标签
_ENGINE_LABELS: dict[str, str] = {
    "cycle": "周期阶段",
    "valuation": "估值状态",
    "risk": "风险等级",
    "regime": "综合状态",
}

#: 默认摘要配置（DB 无记录时使用）
_DEFAULT_DIGEST_CONFIG: dict[str, Any] = {
    "enabled": False,
    "daily_hour": 8,
    "weekly_day": 0,
    "weekly_hour": 9,
    "recipients": [],
    "last_daily_sent": None,
    "last_weekly_sent": None,
}

QUOTE_CCY = "USDT"


def _local_now() -> datetime:
    """按调度时区取当前时间（时区非法回退 UTC）。"""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(settings.scheduler_timezone))
    except Exception:  # noqa: BLE001
        return datetime.now(UTC)


def _fmt_money_usd(v: float | None) -> str | None:
    """美元金额格式化（亿）。"""
    if v is None:
        return None
    return f"{v / 1e8:,.2f} 亿美元"


def _fmt_price(v: float | None) -> str | None:
    if v is None:
        return None
    return f"{v:,.2f} USDT"


class DigestService:
    """每日摘要 + 周报。"""

    def __init__(self) -> None:
        self._jinja_env: Environment | None = None

    # ------------------------------------------------------------------
    # 每日摘要
    # ------------------------------------------------------------------

    async def generate_daily_data(self) -> dict:
        """收集每日摘要数据（单项失败降级，不抛出）。"""
        price_row = await self._load_price()
        current_price = price_row.get("price")
        changes = await self._load_changes(current_price)
        engines = await self._load_engines()
        indicators = await self._load_indicators(_DAILY_INDICATOR_CODES)
        etf = await self._load_etf(days=7)
        onchain = await self._load_onchain()
        changes_24h = self._collect_daily_changes(engines, etf, changes)
        watch_rules = await self._load_watch_rules()

        return {
            "generated_at": _local_now().strftime("%Y-%m-%d %H:%M %Z"),
            "price": {
                "current": _fmt_price(current_price),
                "change_24h": changes.get("24h"),
                "change_7d": changes.get("7d"),
            },
            "engines": engines.get("current", {}),
            "indicators": indicators,
            "etf": etf,
            "onchain": onchain,
            "changes_24h": changes_24h,
            "watch_rules": watch_rules,
        }

    async def send_daily_digest(self, recipients: list[str] | None = None) -> dict:
        """发送每日摘要（默认收件人 = settings.alert_default_recipient）。"""
        to_list = self._resolve_recipients(recipients)
        if not to_list:
            return {"sent": False, "reason": "未配置收件人（alert_default_recipient）"}

        data = await self.generate_daily_data()
        subject = (
            f"【每日摘要】BTC {_fmt_price_text(data['price']['current'])} · "
            f"{_local_now().strftime('%Y-%m-%d')}"
        )
        html = self._render("daily_digest.html.j2", **data)
        text = self._daily_text(data)

        channel: EmailChannel = self._email_channel()
        results = []
        for to in to_list:
            result = await channel.send(to, subject, html, text)
            results.append({"recipient": to, **result.__dict__})
        ok_count = sum(1 for r in results if r["success"])
        logger.info(f"DigestService: 每日摘要发送完成 {ok_count}/{len(results)}")
        return {"sent": ok_count > 0, "results": results}

    # ------------------------------------------------------------------
    # 周报
    # ------------------------------------------------------------------

    async def send_weekly_report(self, recipients: list[str] | None = None) -> dict:
        """发送周报。"""
        to_list = self._resolve_recipients(recipients)
        if not to_list:
            return {"sent": False, "reason": "未配置收件人（alert_default_recipient）"}

        data = await self.generate_weekly_data()
        subject = (
            f"【周报】BTC 周涨跌 {_pct_text(data['performance'].get('change_pct'))} · "
            f"{data['period']['start']} ~ {data['period']['end']}"
        )
        html = self._render("weekly_report.html.j2", **data)
        text = self._weekly_text(data)

        channel: EmailChannel = self._email_channel()
        results = []
        for to in to_list:
            result = await channel.send(to, subject, html, text)
            results.append({"recipient": to, **result.__dict__})
        ok_count = sum(1 for r in results if r["success"])
        logger.info(f"DigestService: 周报发送完成 {ok_count}/{len(results)}")
        return {"sent": ok_count > 0, "results": results}

    async def generate_weekly_data(self) -> dict:
        """收集周报数据（单项失败降级）。"""
        now = datetime.now(UTC)
        week_start = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        performance = await self._load_week_performance(week_start, now)
        engines_changes = await self._load_engine_changes(week_start)
        etf_week = await self._load_etf_week(week_start, now)
        portfolio = await self._load_portfolio_summary(week_start, now)

        return {
            "platform_name": settings.app_name,
            "platform_url": settings.platform_url.rstrip("/"),
            "period": {
                "start": week_start.strftime("%Y-%m-%d"),
                "end": now.strftime("%Y-%m-%d"),
            },
            "performance": performance,
            "engines_changes": engines_changes,
            "etf_week": etf_week,
            "portfolio": portfolio,
        }

    # ------------------------------------------------------------------
    # 调度（maintenance task 调用）
    # ------------------------------------------------------------------

    def _should_send_at(self, hour: int) -> bool:
        """检查当前（调度时区）小时是否到达配置的发送时间。"""
        try:
            return _local_now().hour == int(hour)
        except (TypeError, ValueError):
            return False

    async def maybe_send_scheduled(self) -> dict:
        """按配置检查并自动发送每日摘要 / 周报（防重复：按日去重）。"""
        config = await self._load_digest_config()
        if not config.get("enabled"):
            return {"skipped": "digest disabled"}

        now_local = _local_now()
        actions: dict[str, Any] = {}

        daily_hour = int(config.get("daily_hour") or 8)
        if self._should_send_at(daily_hour):
            today = now_local.strftime("%Y-%m-%d")
            if config.get("last_daily_sent") != today:
                actions["daily"] = await self.send_daily_digest(
                    config.get("recipients") or None
                )
                config["last_daily_sent"] = today
                await self._save_digest_config(config)

        weekly_day = int(config.get("weekly_day") or 0)
        weekly_hour = int(config.get("weekly_hour") or 9)
        if now_local.weekday() == weekly_day and self._should_send_at(weekly_hour):
            week_key = f"{now_local.isocalendar().year}-W{now_local.isocalendar().week:02d}"
            if config.get("last_weekly_sent") != week_key:
                actions["weekly"] = await self.send_weekly_report(
                    config.get("recipients") or None
                )
                config["last_weekly_sent"] = week_key
                await self._save_digest_config(config)

        return actions or {"skipped": "not scheduled time"}

    async def get_digest_config(self) -> dict:
        """读取摘要配置（密码类字段不涉及；供 API 使用）。"""
        return await self._load_digest_config()

    async def update_digest_config(self, updates: dict) -> dict:
        """更新摘要配置（合并保存；供 API 使用）。"""
        config = await self._load_digest_config()
        for key in (
            "enabled", "daily_hour", "weekly_day", "weekly_hour", "recipients",
        ):
            if key in updates and updates[key] is not None:
                config[key] = updates[key]
        await self._save_digest_config(config)
        return config

    # ------------------------------------------------------------------
    # 数据采集（全部 try/except 优雅降级）
    # ------------------------------------------------------------------

    def _get_market_service(self) -> Any:
        """惰性获取 MarketService。"""
        from app.services.market_service import MarketService
        from app.services.provider_service import get_provider_service

        return MarketService(get_provider_service().manager)

    async def _load_price(self) -> dict:
        """当前价格（市场服务优先，K 线回退）。"""
        try:
            service = self._get_market_service()
            result = await service.get_current_price("BTCUSDT")
            if result.success and result.data:
                price = float(result.data.get("price") or 0) or None
                if price:
                    return {"price": price, "source": result.source}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"DigestService: 当前价获取失败: {e}")
        closes = await self._load_closes(CandleInterval.H1, 2)
        if closes:
            return {"price": closes[-1][1], "source": "candles_db"}
        return {"price": None, "source": None}

    async def _load_changes(self, current_price: float | None) -> dict[str, float]:
        """24H / 7D 涨跌幅（candles 表计算，%）。"""
        changes: dict[str, float] = {}
        if current_price is None:
            return changes
        try:
            hourly = await self._load_closes(CandleInterval.H1, 48)
            daily = await self._load_closes(CandleInterval.D1, 40)
            now = datetime.now(UTC)
            for label, hours, series in (
                ("24h", 24, hourly), ("7d", 24 * 7, daily), ("30d", 24 * 30, daily),
            ):
                past = self._close_at(series, now - timedelta(hours=hours))
                if past:
                    changes[label] = round((current_price - past) / past * 100, 2)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"DigestService: 涨跌幅计算失败: {e}")
        return changes

    async def _load_closes(
        self, interval: CandleInterval, limit: int
    ) -> list[tuple[datetime, float]]:
        """K 线收盘序列（升序；失败返回空）。"""
        pair = to_compact(f"BTC/{QUOTE_CCY}")
        try:
            async with get_db_session_ctx() as session:
                rows = (
                    await session.execute(
                        select(Candle)
                        .where(Candle.symbol == pair, Candle.interval == interval)
                        .order_by(Candle.observation_time.desc())
                        .limit(limit)
                    )
                ).scalars().all()
            return [
                (row.observation_time, float(row.close)) for row in reversed(rows)
            ]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"DigestService: K 线加载失败 ({interval.value}): {e}")
            return []

    @staticmethod
    def _close_at(series: list[tuple[datetime, float]], target: datetime) -> float | None:
        """时间戳 <= target 的最近收盘价。"""
        for ts, close in reversed(series):
            ts_utc = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
            if ts_utc <= target:
                return close
        return None

    async def _load_engines(self) -> dict:
        """四引擎最新状态（current/previous，供变化对比）。"""
        sources: dict[str, tuple[type, str]] = {
            "cycle": (CycleState, "phase"),
            "valuation": (ValuationState, "valuation_level"),
            "risk": (RiskScore, "risk_level"),
            "regime": (MarketRegime, "overall_regime"),
        }
        current: dict[str, str] = {}
        previous: dict[str, str] = {}
        for name, (model, field) in sources.items():
            try:
                async with get_db_session_ctx() as session:
                    rows = (
                        await session.execute(
                            select(model)
                            .order_by(model.observation_time.desc())
                            .limit(2)
                        )
                    ).scalars().all()
                if rows:
                    cur = getattr(rows[0], field)
                    current[name] = cur.value if hasattr(cur, "value") else str(cur)
                    if len(rows) > 1:
                        prev = getattr(rows[1], field)
                        previous[name] = prev.value if hasattr(prev, "value") else str(prev)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"DigestService: 引擎 {name} 状态加载失败: {e}")
        return {"current": current, "previous": previous}

    async def _load_indicators(self, codes: tuple[str, ...]) -> list[dict]:
        """指标最新值 + 分位 + 信号。"""
        items: list[dict] = []
        for code in codes:
            try:
                async with get_db_session_ctx() as session:
                    row = (
                        await session.execute(
                            select(IndicatorValue)
                            .join(
                                IndicatorDefinition,
                                IndicatorValue.indicator_id == IndicatorDefinition.id,
                            )
                            .where(
                                IndicatorDefinition.code == code,
                                IndicatorValue.symbol == "BTC",
                            )
                            .order_by(IndicatorValue.observation_time.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                if row is None:
                    continue
                items.append({
                    "label": _INDICATOR_LABELS.get(code, code),
                    "value": _fmt_number(float(row.value), code),
                    "percentile": (
                        f"{float(row.percentile):.0f}%"
                        if row.percentile is not None else None
                    ),
                    "signal": row.signal,
                })
            except Exception as e:  # noqa: BLE001
                logger.warning(f"DigestService: 指标 {code} 加载失败: {e}")
        return items

    async def _load_etf(self, days: int = 7) -> dict:
        """ETF 资金流（最新交易日 + 近 N 日合计，亿美元）。"""
        try:
            since = datetime.now(UTC) - timedelta(days=days)
            async with get_db_session_ctx() as session:
                rows = (
                    await session.execute(
                        select(EtfFlow)
                        .where(EtfFlow.observation_time >= since)
                        .order_by(EtfFlow.observation_time.desc())
                    )
                ).scalars().all()
            if not rows:
                return {}
            latest_day = max(r.observation_time for r in rows)
            last_flow = _sum_flows(
                r.net_flow_usd for r in rows
                if _same_day(r.observation_time, latest_day)
            )
            total_flow = _sum_flows(r.net_flow_usd for r in rows)
            return {
                "last_net_flow": _fmt_money_usd(last_flow),
                "net_flow_7d": _fmt_money_usd(total_flow),
                "_last_flow_raw": last_flow,
                "_total_flow_raw": total_flow,
            }
        except Exception as e:  # noqa: BLE001
            logger.warning(f"DigestService: ETF 资金流加载失败: {e}")
            return {}

    async def _load_etf_week(self, start: datetime, end: datetime) -> dict:
        """ETF 周度资金流统计。"""
        try:
            async with get_db_session_ctx() as session:
                rows = (
                    await session.execute(
                        select(EtfFlow).where(
                            EtfFlow.observation_time >= start,
                            EtfFlow.observation_time <= end,
                        )
                    )
                ).scalars().all()
            if not rows:
                return {}
            total = _sum_flows(r.net_flow_usd for r in rows)
            days = {r.observation_time.date() for r in rows}
            inflow_days = sum(
                1 for d in days
                if _sum_flows(
                    r.net_flow_usd for r in rows if r.observation_time.date() == d
                ) > 0
            )
            return {
                "net_flow_total": _fmt_money_usd(total),
                "flow_days": len(days),
                "inflow_days": inflow_days,
            }
        except Exception as e:  # noqa: BLE001
            logger.warning(f"DigestService: ETF 周度统计失败: {e}")
            return {}

    async def _load_onchain(self) -> list[dict]:
        """链上指标摘要（最新值）。"""
        items: list[dict] = []
        for name in _ONCHAIN_METRICS:
            try:
                async with get_db_session_ctx() as session:
                    row = (
                        await session.execute(
                            select(OnchainMetric)
                            .where(OnchainMetric.metric_name == name)
                            .order_by(OnchainMetric.observation_time.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                if row is not None:
                    items.append({
                        "label": name,
                        "value": f"{float(row.value):.4g}",
                        "unit": row.unit or "",
                    })
            except Exception as e:  # noqa: BLE001
                logger.warning(f"DigestService: 链上指标 {name} 加载失败: {e}")
        return items

    async def _load_watch_rules(self) -> list[dict]:
        """需关注的条件：今日触发的规则 / 今日恢复的规则。"""
        out: list[dict] = []
        day_start = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        try:
            async with get_db_session_ctx() as session:
                rows = (
                    await session.execute(
                        select(AlertEvent, AlertRule)
                        .join(AlertRule, AlertEvent.rule_id == AlertRule.id)
                        .where(
                            (AlertEvent.triggered_at >= day_start)
                            | (AlertEvent.resolved_at >= day_start)
                        )
                        .order_by(AlertEvent.triggered_at.desc())
                        .limit(10)
                    )
                ).all()
            for event, rule in rows:
                if event.status in (AlertStatus.TRIGGERED, AlertStatus.ACKED):
                    status = "今日已触发"
                elif event.status == AlertStatus.RESOLVED:
                    status = "已恢复"
                else:
                    status = event.status.value
                out.append({
                    "rule_name": rule.rule_name,
                    "condition_text": rule.condition_text or rule.rule_name,
                    "status": status,
                })
        except Exception as e:  # noqa: BLE001
            logger.warning(f"DigestService: 关注规则加载失败: {e}")
        return out

    async def _load_week_performance(
        self, start: datetime, end: datetime
    ) -> dict:
        """周表现（最高/最低/涨跌幅）。"""
        closes = await self._load_closes(CandleInterval.D1, 40)
        window = [
            (ts, close) for ts, close in closes
            if start <= (ts if ts.tzinfo else ts.replace(tzinfo=UTC)) <= end
        ]
        if not window:
            return {"high": None, "low": None, "change_pct": None, "close": None}
        highs = max(c for _, c in window)
        lows = min(c for _, c in window)
        first, last = window[0][1], window[-1][1]
        change = round((last - first) / first * 100, 2) if first else None
        return {
            "high": _fmt_price(highs),
            "low": _fmt_price(lows),
            "change_pct": change,
            "close": _fmt_price(last),
        }

    async def _load_engine_changes(self, week_start: datetime) -> list[dict]:
        """周度引擎状态变化（当前 vs 上期）。"""
        engines = await self._load_engines()
        current = engines.get("current", {})
        previous = engines.get("previous", {})
        rows: list[dict] = []
        for name, label in _ENGINE_LABELS.items():
            cur, prev = current.get(name), previous.get(name)
            if cur is None:
                continue
            rows.append({
                "label": label,
                "from": prev,
                "to": cur,
                "changed": bool(prev and cur and prev != cur),
            })
        return rows

    async def _load_portfolio_summary(
        self, start: datetime, end: datetime
    ) -> dict | None:
        """投资计划执行情况（最新组合快照，无数据返回 None）。"""
        try:
            async with get_db_session_ctx() as session:
                row = (
                    await session.execute(
                        select(PortfolioSnapshot)
                        .order_by(PortfolioSnapshot.observation_time.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
            if row is None:
                return None
            cumulative = (
                f"{float(row.cumulative_return) * 100:+.2f}%"
                if row.cumulative_return is not None else None
            )
            return {
                "total_value": f"{float(row.total_value):,.2f} USDT",
                "cumulative_return": cumulative,
                "daily_plan_text": None,
            }
        except Exception as e:  # noqa: BLE001
            logger.warning(f"DigestService: 组合快照加载失败: {e}")
            return None

    # ------------------------------------------------------------------
    # 摘要文案组装
    # ------------------------------------------------------------------

    def _collect_daily_changes(
        self, engines: dict, etf: dict, changes: dict[str, float]
    ) -> list[dict]:
        """过去 24h 重要变化列表。"""
        out: list[dict] = []
        cur = engines.get("current", {})
        prev = engines.get("previous", {})
        for name in ("cycle", "valuation", "risk", "regime"):
            c, p = cur.get(name), prev.get(name)
            if c and p and c != p:
                out.append({
                    "label": _ENGINE_LABELS.get(name, name),
                    "text": f"{p} → {c}",
                })
        raw_last = etf.get("_last_flow_raw")
        if raw_last is not None and abs(raw_last) >= 2e8:  # 单日 ≥2 亿美元视为异常
            direction = "流入" if raw_last > 0 else "流出"
            out.append({
                "label": "ETF 资金异常",
                "text": f"单日净{direction} {_fmt_money_usd(abs(raw_last))}",
            })
        chg = changes.get("24h")
        if chg is not None and abs(chg) >= 5:
            out.append({
                "label": "价格剧烈波动",
                "text": f"24H 涨跌幅 {chg:+.2f}%",
            })
        return out

    # ------------------------------------------------------------------
    # 渲染与发送
    # ------------------------------------------------------------------

    def _get_jinja_env(self) -> Environment:
        if self._jinja_env is None:
            self._jinja_env = Environment(
                loader=FileSystemLoader(str(_TEMPLATE_DIR)),
                autoescape=False,  # 模板内容为服务端受控数据，手动保证安全
                trim_blocks=True,
                lstrip_blocks=True,
            )
        return self._jinja_env

    def _render(self, template: str, **variables: Any) -> str:
        return self._get_jinja_env().get_template(template).render(**variables)

    def _email_channel(self) -> EmailChannel:
        channel = ChannelRegistry.get("EMAIL")
        if channel is None:
            channel = EmailChannel()
        return channel  # type: ignore[return-value]

    def _resolve_recipients(self, recipients: list[str] | None) -> list[str]:
        if recipients:
            return [r for r in recipients if r]
        from app.services.settings_service import get_settings_service

        # SettingsService(alert.default_recipient) > env（get_sync 同步读，热生效）
        default = get_settings_service().get_sync(
            "alert.default_recipient", None
        ) or settings.alert_default_recipient
        return [default] if default else []

    def _daily_text(self, data: dict) -> str:
        """每日摘要纯文本版。"""
        price = data["price"]
        engines = data["engines"]
        lines = [
            "=" * 52,
            "BTC 每日市场摘要",
            data["generated_at"],
            "=" * 52,
            "",
            f"当前价格：{price['current'] or '—'}"
            f"{'' if price['change_24h'] is None else f'（24H {price['change_24h']:+.2f}%）'}",
            "",
            "综合状态：",
            f"  市场阶段：{engines.get('cycle_phase') or '—'}",
            f"  估值状态：{engines.get('valuation_level') or '—'}",
            f"  风险等级：{engines.get('risk_level') or '—'}",
            f"  综合状态：{engines.get('overall_regime') or '—'}",
        ]
        if data["indicators"]:
            lines.append("")
            lines.append("关键指标：")
            for m in data["indicators"]:
                lines.append(
                    f"  - {m['label']}：{m['value']}"
                    f"{'' if not m['percentile'] else f'（分位 {m['percentile']}）'}"
                )
        if data["changes_24h"]:
            lines.append("")
            lines.append("过去 24 小时重要变化：")
            for ch in data["changes_24h"]:
                lines.append(f"  - {ch['label']}：{ch['text']}")
        if data["watch_rules"]:
            lines.append("")
            lines.append("需关注的监测条件：")
            for r in data["watch_rules"]:
                lines.append(f"  - {r['rule_name']}（{r['status']}）")
        lines += [
            "",
            "-" * 52,
            f"查看详情：{settings.platform_url.rstrip('/')}",
            f"{settings.app_name} · 本邮件由系统自动发送",
        ]
        return "\n".join(lines)

    def _weekly_text(self, data: dict) -> str:
        """周报纯文本版。"""
        perf = data["performance"]
        lines = [
            "=" * 52,
            f"BTC 每周市场报告（{data['period']['start']} ~ {data['period']['end']}）",
            "=" * 52,
            "",
            "本周表现：",
            f"  最高：{perf.get('high') or '—'}",
            f"  最低：{perf.get('low') or '—'}",
            f"  周涨跌幅：{_pct_text(perf.get('change_pct'))}",
        ]
        if data["engines_changes"]:
            lines.append("")
            lines.append("维度变化：")
            for e in data["engines_changes"]:
                mark = "（变化）" if e["changed"] else ""
                lines.append(f"  - {e['label']}：{e['from'] or '—'} → {e['to'] or '—'} {mark}")
        if data["etf_week"]:
            lines.append("")
            lines.append(f"ETF 周度净流入：{data['etf_week'].get('net_flow_total') or '—'}")
        if data["portfolio"]:
            lines.append("")
            lines.append("投资计划执行情况：")
            lines.append(f"  组合总值：{data['portfolio'].get('total_value') or '—'}")
            lines.append(f"  累计收益率：{data['portfolio'].get('cumulative_return') or '—'}")
        lines += [
            "",
            "-" * 52,
            f"查看详情：{settings.platform_url.rstrip('/')}",
            f"{settings.app_name} · 本邮件由系统自动发送",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 配置读写
    # ------------------------------------------------------------------

    async def _load_digest_config(self) -> dict:
        """读取摘要配置（DB 系统级 DIGEST 行；失败回退默认）。"""
        try:
            async with get_db_session_ctx() as session:
                row = (
                    await session.execute(
                        select(AlertChannelConfig)
                        .where(
                            AlertChannelConfig.channel_type == "DIGEST",
                            AlertChannelConfig.user_id.is_(None),  # type: ignore[union-attr]
                        )
                        .order_by(AlertChannelConfig.updated_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
            if row is None:
                return dict(_DEFAULT_DIGEST_CONFIG)
            return {**_DEFAULT_DIGEST_CONFIG, **dict(row.config)}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"DigestService: 摘要配置读取失败，回退默认: {e}")
            return dict(_DEFAULT_DIGEST_CONFIG)

    async def _save_digest_config(self, config: dict) -> None:
        """保存摘要配置（upsert 系统级 DIGEST 行）。"""
        try:
            async with get_db_session_ctx() as session:
                row = (
                    await session.execute(
                        select(AlertChannelConfig)
                        .where(
                            AlertChannelConfig.channel_type == "DIGEST",
                            AlertChannelConfig.user_id.is_(None),  # type: ignore[union-attr]
                        )
                        .order_by(AlertChannelConfig.updated_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if row is None:
                    session.add(
                        AlertChannelConfig(
                            channel_type="DIGEST",
                            channel_name="摘要订阅配置",
                            config=config,
                            is_enabled=True,
                        )
                    )
                else:
                    row.config = config
        except Exception:  # noqa: BLE001
            logger.exception("DigestService: 摘要配置保存失败")


# ----------------------------------------------------------------------
# 辅助函数
# ----------------------------------------------------------------------


def _fmt_number(v: float, code: str) -> str:
    """按指标类型格式化数值。"""
    if code == "funding_rate":
        return f"{v * 100:.3f}%"
    if code in {"mvrv", "sopr", "nupl"}:
        return f"{v:.2f}"
    return f"{v:g}"


def _fmt_price_text(price: str | None) -> str:
    """邮件主题中的价格（已格式化字符串截断空格）。"""
    return price or "价格不可用"


def _pct_text(v: float | None) -> str:
    return "—" if v is None else f"{v:+.2f}%"


def _sum_flows(values) -> float | None:
    """net_flow_usd 求和（空返回 None）。"""
    total = sum(float(v) for v in values if v is not None)
    return total


def _same_day(a: datetime, b: datetime) -> bool:
    a_utc = a if a.tzinfo else a.replace(tzinfo=UTC)
    b_utc = b if b.tzinfo else b.replace(tzinfo=UTC)
    return a_utc.date() == b_utc.date()


_digest_service: DigestService | None = None


def get_digest_service() -> DigestService:
    """获取全局 DigestService 单例。"""
    global _digest_service
    if _digest_service is None:
        _digest_service = DigestService()
    return _digest_service
