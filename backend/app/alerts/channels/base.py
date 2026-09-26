"""通知渠道抽象基类。

定义统一的通知发送接口，供 :class:`app.alerts.dispatcher.AlertDispatcher`
按 ``rule.channels`` 列出的渠道类型分发投递。

预留扩展：TelegramChannel / WebhookChannel 只需继承本基类并注册到
:class:`~app.alerts.channels.registry.ChannelRegistry` 即可接入，
无需改动 dispatcher 主流程。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SendResult:
    """渠道发送结果（统一结构，供投递记录与 API 响应使用）。"""

    success: bool
    message_id: str | None = None
    error: str | None = None
    response: dict = field(default_factory=dict)


class NotificationChannel(ABC):
    """通知渠道抽象基类（预留 Telegram/Webhook 扩展）。"""

    @property
    @abstractmethod
    def channel_type(self) -> str:
        """渠道类型标识（EMAIL / WEBHOOK / TELEGRAM，与 rule.channels 取值一致）。"""

    @abstractmethod
    async def send(
        self,
        recipient: str,
        subject: str,
        body_html: str,
        body_text: str,
        config: dict | None = None,
    ) -> SendResult:
        """发送通知。

        Args:
            recipient: 接收方（邮箱地址 / webhook URL / chat id）
            subject: 通知标题
            body_html: HTML 正文
            body_text: 纯文本正文
            config: 渠道级配置覆盖（自定义 host/port/凭据等），None 用全局默认

        Returns:
            SendResult：成功与否 + 错误分类 + 渠道响应详情
        """

    @abstractmethod
    async def test(
        self,
        recipient: str,
        config: dict | None = None,
    ) -> SendResult:
        """发送测试通知（渠道连通性验证，供 API「测试渠道」端点调用）。"""
