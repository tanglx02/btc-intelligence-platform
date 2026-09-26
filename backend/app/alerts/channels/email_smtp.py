"""SMTP 邮件渠道（aiosmtplib）。

TLS 模式约定：
- ``use_ssl=True``  -> 隐式 SSL（通常 465 端口，连接即 TLS 握手）；
- ``use_tls=True``  -> STARTTLS（通常 587 端口，明文连接后升级 TLS）；
- 两者同时为 True 时优先隐式 SSL；都为 False 时明文（仅内网测试用）。

错误分类：连接失败 / 认证失败 / 超时 / 收件人拒绝 / 其他，
统一封装为 :class:`~app.alerts.channels.base.SendResult`，不向调用方抛异常。
"""

from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any

import aiosmtplib
import aiosmtplib.errors
from loguru import logger

from app.alerts.channels.base import NotificationChannel, SendResult
from app.core.config import settings


@dataclass(frozen=True)
class _SmtpConfig:
    """归一化后的 SMTP 发送配置（全局 settings + 渠道级覆盖合并结果）。"""

    enabled: bool
    host: str
    port: int
    user: str
    password: str
    from_email: str
    from_name: str
    use_tls: bool
    use_ssl: bool
    timeout: int


def _to_bool(value: Any) -> bool:
    """宽松布尔解析（DB JSON / API 传入 "true"/"1"/True 均可）。"""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


class EmailChannel(NotificationChannel):
    """SMTP 邮件渠道。

    配置合并优先级：``send(config=...)`` 渠道级覆盖 > DB 运行时配置（由
    dispatcher 注入）> ``settings.smtp_*`` 环境变量默认值。
    """

    @property
    def channel_type(self) -> str:
        return "EMAIL"

    # ------------------------------------------------------------------
    # 配置合并
    # ------------------------------------------------------------------

    def _merge_config(self, config: dict | None = None) -> _SmtpConfig:
        """合并全局设置与渠道级覆盖（config 中非空键优先）。"""
        c = config or {}
        use_tls = _to_bool(c.get("use_tls", settings.smtp_use_tls))
        use_ssl = _to_bool(c.get("use_ssl", settings.smtp_use_ssl))
        # 同时开启时隐式 SSL 优先（465）
        if use_ssl:
            use_tls = False
        return _SmtpConfig(
            enabled=_to_bool(c.get("enabled", settings.smtp_enabled)),
            host=str(c.get("host") or settings.smtp_host),
            port=int(c.get("port") or settings.smtp_port or 587),
            user=str(c.get("user", settings.smtp_user) or ""),
            password=str(c.get("password", settings.smtp_password) or ""),
            from_email=str(c.get("from_email") or settings.smtp_from_email),
            from_name=str(c.get("from_name") or settings.smtp_from_name),
            use_tls=use_tls,
            use_ssl=use_ssl,
            timeout=int(c.get("timeout_seconds") or settings.smtp_timeout_seconds or 15),
        )

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------

    async def send(
        self,
        recipient: str,
        subject: str,
        body_html: str,
        body_text: str,
        config: dict | None = None,
    ) -> SendResult:
        """发送 MIME multipart/alternative 邮件（text + html 两部分）。"""
        cfg = self._merge_config(config)
        if not cfg.enabled:
            return SendResult(
                success=False,
                error="SMTP 未启用（smtp_enabled=false 或渠道配置禁用）",
                response={"category": "DISABLED"},
            )
        if not recipient:
            return SendResult(
                success=False,
                error="收件人为空",
                response={"category": "INVALID_RECIPIENT"},
            )

        message = EmailMessage()
        message["From"] = formataddr((cfg.from_name, cfg.from_email))
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(body_text or body_html)
        message.add_alternative(body_html, subtype="html")

        try:
            response = await aiosmtplib.send(
                message,
                hostname=cfg.host,
                port=cfg.port,
                username=cfg.user or None,
                password=cfg.password or None,
                use_tls=cfg.use_ssl,
                start_tls=cfg.use_tls,
                timeout=cfg.timeout,
            )
            message_id = message.get("Message-ID")
            logger.info(f"EmailChannel: 已发送至 {recipient} ({cfg.host}:{cfg.port})")
            return SendResult(
                success=True,
                message_id=str(message_id) if message_id else None,
                response={
                    "category": "OK",
                    "smtp_response": str(response) if response else "",
                    "host": cfg.host,
                    "port": cfg.port,
                },
            )
        except aiosmtplib.errors.SMTPAuthenticationError as e:
            return self._fail(recipient, "认证失败（检查 SMTP_USER/SMTP_PASSWORD）", e)
        except (
            aiosmtplib.errors.SMTPConnectError,
            aiosmtplib.errors.SMTPConnectAuthError,
            ConnectionError,
            OSError,
        ) as e:
            return self._fail(recipient, f"连接失败（{cfg.host}:{cfg.port}）", e)
        except aiosmtplib.errors.SMTPRecipientsRefused as e:
            return self._fail(recipient, f"收件人被拒绝: {recipient}", e)
        except TimeoutError as e:
            # aiosmtplib.errors.SMTPTimeoutError 是 TimeoutError 子类，一并命中
            return self._fail(recipient, f"发送超时（>{cfg.timeout}s）", e)
        except Exception as e:  # noqa: BLE001 - 渠道发送永不抛出，错误入 SendResult
            return self._fail(recipient, "发送失败", e)

    async def test(
        self,
        recipient: str,
        config: dict | None = None,
    ) -> SendResult:
        """发送测试邮件（渠道连通性验证）。"""
        cfg = self._merge_config(config)
        subject = f"【测试】{cfg.from_name} 邮件通道验证"
        body_text = (
            "这是一封测试邮件。\n\n"
            f"SMTP 服务器：{cfg.host}:{cfg.port}\n"
            "收到本邮件说明预警通知渠道配置正确。\n\n"
            f"—— {cfg.from_name}"
        )
        body_html = (
            "<div style='font-family:sans-serif;max-width:520px;margin:0 auto;"
            "padding:24px;border:1px solid #e5e7eb;border-radius:12px'>"
            "<h2 style='margin:0 0 12px;color:#111827'>邮件通道测试</h2>"
            f"<p style='color:#374151'>SMTP 服务器：<b>{cfg.host}:{cfg.port}</b></p>"
            "<p style='color:#374151'>收到本邮件说明预警通知渠道配置正确。</p>"
            f"<p style='color:#6b7280;font-size:13px'>—— {cfg.from_name}</p>"
            "</div>"
        )
        return await self.send(recipient, subject, body_html, body_text, config)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _fail(recipient: str, category_msg: str, exc: Exception) -> SendResult:
        """统一错误封装（记录日志 + 分类信息 + 原始异常文本）。"""
        logger.warning(f"EmailChannel: 发送至 {recipient} 失败 - {category_msg}: {exc}")
        return SendResult(
            success=False,
            error=f"{category_msg}: {exc}",
            response={
                "category": category_msg.split("（")[0],
                "exception": type(exc).__name__,
            },
        )
