"""应用配置中心。

基于 pydantic-settings，从环境变量 / .env 文件加载配置。
所有敏感信息（数据库密码、API Key）均通过环境变量注入，禁止硬编码。

约定：环境变量名与字段名大小写不敏感匹配，嵌套配置以前缀区分，
例如 POSTGRES_HOST、REDIS_URL、PROVIDER_COINGLASS_KEY 等。
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局应用配置（占位骨架，字段随功能迭代补充）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- 应用基础配置 ----
    app_name: str = Field(default="BTC Intelligence Platform")
    app_env: str = Field(default="development", description="运行环境: development / staging / production")
    debug: bool = Field(default=True)
    api_v1_prefix: str = Field(default="/api/v1")
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000)

    # ---- CORS ----
    # 逗号分隔来源列表；生产经 nginx 同源反代，通常无需扩展
    cors_origins: str = Field(default="http://localhost:3000,http://localhost:8080")

    # ---- 安全 / 鉴权 ----
    secret_key: str = Field(default="change-me-in-production")
    access_token_expire_minutes: int = Field(default=60 * 24)
    algorithm: str = Field(default="HS256")
    # DB 敏感配置（system_settings / providers.api_key_encrypted）加密密钥；
    # 未设置时从 SECRET_KEY 经 HKDF 派生（见 app.core.crypto）
    settings_encryption_key: str = Field(default="")

    # ---- 数据库 (PostgreSQL + TimescaleDB) ----
    postgres_host: str = Field(default="localhost")
    postgres_port: int = Field(default=5432)
    postgres_db: str = Field(default="btc_platform")
    postgres_user: str = Field(default="btc_admin")
    postgres_password: str = Field(default="changeme")

    # ---- 缓存 (Redis) ----
    redis_host: str = Field(default="localhost")
    redis_port: int = Field(default=6379)
    redis_password: str = Field(default="changeme")
    redis_db: int = Field(default=0)

    # ---- 外部数据源 API Keys（占位，按需填充）----
    provider_binance_key: str | None = None
    provider_coinbase_key: str | None = None
    provider_coinglass_key: str | None = None
    provider_glassnode_key: str | None = None
    provider_cryptoquant_key: str | None = None
    provider_farside_key: str | None = None
    provider_deribit_key: str | None = None
    provider_fred_key: str | None = None
    provider_alternative_key: str | None = None

    # ---- 调度器 ----
    scheduler_enabled: bool = Field(default=True)
    scheduler_timezone: str = Field(default="Asia/Shanghai")

    # ---- SMTP 邮件通知 ----
    smtp_enabled: bool = Field(default=False)
    smtp_host: str = Field(default="smtp.gmail.com")
    smtp_port: int = Field(default=587)
    smtp_user: str = Field(default="")
    smtp_password: str = Field(default="")
    smtp_from_email: str = Field(default="alerts@btc-platform.local")
    smtp_from_name: str = Field(default="BTC Intelligence Platform")
    smtp_use_tls: bool = Field(default=True)
    smtp_use_ssl: bool = Field(default=False)
    smtp_timeout_seconds: int = Field(default=15)

    # ---- Alert 引擎 ----
    alert_enabled: bool = Field(default=True)
    alert_scan_interval_seconds: int = Field(default=60)
    alert_default_recipient: str = Field(default="")  # MVP 单收件人
    alert_cooldown_default_seconds: int = Field(default=3600)
    alert_max_per_hour: int = Field(default=20)

    # ---- 平台入口（邮件中「查看详情」链接）----
    platform_url: str = Field(default="http://localhost:3000")

    # ---- 派生属性：连接 URL ----
    @property
    def database_url(self) -> str:
        """异步 SQLAlchemy 数据库连接 URL（asyncpg 驱动）。"""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def database_url_sync(self) -> str:
        """同步数据库连接 URL，供 Alembic 迁移使用。"""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        """Redis 连接 URL。"""
        return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/{self.redis_db}"


@lru_cache
def get_settings() -> Settings:
    """返回带缓存的全局配置单例。"""
    return Settings()


settings = get_settings()
