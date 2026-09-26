"""Provider 配置数据类。

定义 ProviderConfig 及其加载逻辑，支持从 YAML 文件加载配置，
并通过环境变量插值解析敏感信息（API Key 等）。
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class TimeoutConfig:
    """超时配置。"""

    connect: float = 5.0
    read: float = 10.0
    write: float = 10.0
    pool: float = 10.0


@dataclass
class RetryConfig:
    """重试配置。"""

    count: int = 3
    backoff_factor: float = 2.0
    max_delay: float = 60.0
    jitter: bool = True
    retryable_status_codes: set[int] = field(
        default_factory=lambda: {429, 500, 502, 503, 504}
    )


@dataclass
class RateLimitConfig:
    """限流配置。"""

    requests: int = 60
    period: int = 60  # 窗口秒数
    burst: int | None = None  # 突发允许数量，None 时等于 requests


@dataclass
class ProviderConfig:
    """Provider 完整配置数据类。

    每个 Provider 实例持有一个独立的配置，包含网络、认证、限流、重试等全部参数。

    Attributes:
        name: Provider 唯一名称标识
        category: 数据类别 (market / onchain / etf / derivatives / options / macro / sentiment)
        enabled: 是否启用
        priority: 优先级（数值越小越优先）
        base_url: API 基础 URL
        api_key: API 密钥
        api_secret: API Secret
        api_passphrase: 部分交易所需要 passphrase（如 OKX）
        timeout: 超时配置
        retry: 重试配置
        rate_limit: 限流配置
        proxy: 代理地址（http:// / https:// / socks5://），None 表示直连
        headers: 自定义请求头
        keep_alive: 是否启用 HTTP Keep-Alive
        max_connections: 连接池最大连接数
        verify_ssl: 是否验证 SSL 证书
        supported_symbols: 支持的交易对列表
        supported_intervals: 支持的 K 线间隔
        websocket_url: WebSocket 地址（可选）
        extra: 扩展配置（Provider 特有参数）
        locked: 是否锁定优先级（管理员配置）
        description: Provider 描述
        health_check_interval: 健康检查间隔（秒）
        health_check_timeout: 健康检查超时（秒）
    """

    name: str
    category: str = "market"
    enabled: bool = True
    priority: int = 100
    base_url: str = ""
    api_key: str | None = None
    api_secret: str | None = None
    api_passphrase: str | None = None

    timeout: TimeoutConfig = field(default_factory=TimeoutConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)

    proxy: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    keep_alive: bool = True
    max_connections: int = 20
    verify_ssl: bool = True

    supported_symbols: list[str] = field(default_factory=lambda: ["BTC/USDT"])
    supported_intervals: list[str] = field(default_factory=list)
    websocket_url: str | None = None

    extra: dict[str, Any] = field(default_factory=dict)
    locked: bool = False
    description: str = ""

    health_check_interval: int = 60
    health_check_timeout: int = 10


class ConfigLoader:
    """Provider 配置加载器。

    支持从 YAML 文件加载配置，并通过环境变量引用语法解析敏感信息：
    - ``${VAR_NAME}``              — 直接取环境变量，未设置时替换为空字符串
    - ``${VAR_NAME:-FALLBACK_VAR}`` — 优先取 VAR_NAME，未设置时回退取 FALLBACK_VAR
    """

    ENV_PATTERN = re.compile(r"\$\{(\w+)(?::-([\w:.-]*))?\}")

    @classmethod
    def load(cls, config_path: str | Path = "config/providers.yaml") -> dict[str, Any]:
        """加载 YAML 配置并解析环境变量引用。

        Args:
            config_path: YAML 配置文件路径

        Returns:
            解析后的配置字典
        """
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Provider 配置文件不存在: {path}")

        raw = path.read_text(encoding="utf-8")
        resolved = cls._resolve_env_vars(raw)
        return yaml.safe_load(resolved) or {}

    @classmethod
    def _resolve_env_vars(cls, text: str) -> str:
        """将 ${VAR_NAME} / ${VAR_NAME:-FALLBACK} 替换为环境变量值。

        未设置的环境变量替换为空字符串（不抛异常）。
        """

        def replacer(match: re.Match) -> str:
            var_name = match.group(1)
            fallback = match.group(2)
            value = os.environ.get(var_name, "")
            if not value and fallback:
                # 回退支持链式引用（如 ${A:-B}，B 本身也可为环境变量名）
                value = os.environ.get(fallback, "")
            return value

        return cls.ENV_PATTERN.sub(replacer, text)

    @classmethod
    def parse_provider_config(
        cls,
        name: str,
        category: str,
        raw: dict[str, Any],
        defaults: dict[str, Any] | None = None,
    ) -> ProviderConfig:
        """将 YAML 字典解析为 ProviderConfig 实例。

        Args:
            name: Provider 名称
            category: 数据类别
            raw: YAML 中该 Provider 的配置字典
            defaults: 全局默认配置（可被 Provider 级配置覆盖）

        Returns:
            构造好的 ProviderConfig 实例
        """
        merged = {**(defaults or {}), **raw}

        # 解析 timeout 配置
        timeout_raw = merged.get("timeout", {})
        if isinstance(timeout_raw, (int, float)):
            timeout = TimeoutConfig(
                connect=float(timeout_raw),
                read=float(timeout_raw),
                write=float(timeout_raw),
                pool=float(timeout_raw),
            )
        elif isinstance(timeout_raw, dict):
            timeout = TimeoutConfig(
                connect=float(timeout_raw.get("connect", 5.0)),
                read=float(timeout_raw.get("read", 10.0)),
                write=float(timeout_raw.get("write", 10.0)),
                pool=float(timeout_raw.get("pool", 10.0)),
            )
        else:
            timeout = TimeoutConfig()

        # 解析 retry 配置
        retry_raw = merged.get("retry", {})
        if isinstance(retry_raw, dict):
            retry = RetryConfig(
                count=int(retry_raw.get("count", 3)),
                backoff_factor=float(retry_raw.get("backoff_factor", 2.0)),
                max_delay=float(retry_raw.get("max_delay", 60.0)),
                jitter=bool(retry_raw.get("jitter", True)),
            )
        elif isinstance(retry_raw, int):
            retry = RetryConfig(count=retry_raw)
        else:
            retry = RetryConfig()

        # 解析 rate_limit 配置
        rl_raw = merged.get("rate_limit", {})
        if isinstance(rl_raw, dict):
            rate_limit = RateLimitConfig(
                requests=int(rl_raw.get("requests", 60)),
                period=int(rl_raw.get("period", 60)),
                burst=rl_raw.get("burst"),
            )
        elif isinstance(rl_raw, int):
            rate_limit = RateLimitConfig(requests=rl_raw)
        else:
            rate_limit = RateLimitConfig()

        config = ProviderConfig(
            name=name,
            category=category,
            enabled=bool(merged.get("enabled", True)),
            priority=int(merged.get("priority", 100)),
            base_url=str(merged.get("base_url", "")),
            api_key=merged.get("api_key") or None,
            api_secret=merged.get("api_secret") or None,
            api_passphrase=merged.get("api_passphrase") or None,
            timeout=timeout,
            retry=retry,
            rate_limit=rate_limit,
            proxy=merged.get("proxy") or None,
            headers=dict(merged.get("headers", {})),
            keep_alive=bool(merged.get("keep_alive", True)),
            max_connections=int(merged.get("max_connections", 20)),
            verify_ssl=bool(merged.get("verify_ssl", True)),
            supported_symbols=list(merged.get("supported_symbols", ["BTC/USDT"])),
            supported_intervals=list(merged.get("supported_intervals", [])),
            websocket_url=merged.get("websocket_url"),
            extra=dict(merged.get("extra", {})),
            locked=bool(merged.get("locked", False)),
            description=str(merged.get("description", "")),
            health_check_interval=int(merged.get("health_check_interval", 60)),
            health_check_timeout=int(merged.get("health_check_timeout", 10)),
        )

        return config


__all__ = [
    "ConfigLoader",
    "ProviderConfig",
    "RateLimitConfig",
    "RetryConfig",
    "TimeoutConfig",
]
