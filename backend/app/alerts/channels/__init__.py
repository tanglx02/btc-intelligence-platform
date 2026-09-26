"""通知渠道包。

对外导出：渠道抽象基类、SMTP 邮件渠道、渠道注册中心。
``app.alerts.dispatcher`` 通过 :data:`ChannelRegistry` 按渠道类型取实例。
"""

from app.alerts.channels.base import NotificationChannel, SendResult
from app.alerts.channels.email_smtp import EmailChannel
from app.alerts.channels.registry import ChannelRegistry

__all__ = ["ChannelRegistry", "EmailChannel", "NotificationChannel", "SendResult"]
