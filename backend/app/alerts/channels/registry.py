"""通知渠道注册中心。

dispatcher 依据 ``rule.channels`` 中的渠道类型字符串（EMAIL/WEBHOOK/TELEGRAM）
从本注册中心取渠道实例。模块导入时预注册内置 EMAIL 渠道；
Telegram/Webhook 渠道就绪后在此追加注册即可，dispatcher 无需改动。
"""

from __future__ import annotations

from loguru import logger

from app.alerts.channels.base import NotificationChannel


class ChannelRegistry:
    """渠道注册中心（预留扩展）。"""

    _channels: dict[str, NotificationChannel] = {}

    @classmethod
    def register(cls, channel: NotificationChannel) -> None:
        """注册渠道实例（同类型覆盖旧实例，便于测试替换）。"""
        cls._channels[channel.channel_type] = channel
        logger.debug(f"ChannelRegistry: 已注册渠道 {channel.channel_type}")

    @classmethod
    def get(cls, channel_type: str) -> NotificationChannel | None:
        """按渠道类型取实例（未注册返回 None，调用方降级处理）。"""
        return cls._channels.get((channel_type or "").upper())

    @classmethod
    def registered_types(cls) -> list[str]:
        """已注册的渠道类型列表（供 API 展示）。"""
        return sorted(cls._channels.keys())


def _bootstrap() -> None:
    """注册内置渠道（模块导入时执行一次）。"""
    from app.alerts.channels.email_smtp import EmailChannel

    ChannelRegistry.register(EmailChannel())
    # 预留：TelegramChannel, WebhookChannel 注册位
    # ChannelRegistry.register(TelegramChannel())
    # ChannelRegistry.register(WebhookChannel())


_bootstrap()
