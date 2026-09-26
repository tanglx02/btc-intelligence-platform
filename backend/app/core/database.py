"""异步数据库连接管理。

提供 AsyncEngine、AsyncSession 工厂及 FastAPI 依赖注入。
连接池参数可通过环境变量覆盖，默认值适配开发环境。
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

# ---- 连接池配置 ----
POOL_SIZE = 20
MAX_OVERFLOW = 10
POOL_RECYCLE = 3600  # 1小时回收连接，避免 PostgreSQL 超时断开
POOL_PRE_PING = True  # 每次取连接前 ping 一下，确保连接有效
POOL_TIMEOUT = 30  # 等待连接超时（秒）
ECHO_SQL = settings.debug  # 开发模式下打印 SQL

# ---- 全局 AsyncEngine（惰性初始化）----
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """获取全局 AsyncEngine 单例。"""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            pool_size=POOL_SIZE,
            max_overflow=MAX_OVERFLOW,
            pool_recycle=POOL_RECYCLE,
            pool_pre_ping=POOL_PRE_PING,
            pool_timeout=POOL_TIMEOUT,
            echo=ECHO_SQL,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """获取全局 AsyncSession 工厂单例。"""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖注入：提供一个异步数据库会话。

    用法::

        @router.get("/example")
        async def example(session: AsyncSession = Depends(get_db_session)):
            ...
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def get_db_session_ctx() -> AsyncGenerator[AsyncSession, None]:
    """上下文管理器版本的会话获取（用于非 FastAPI 场景，如脚本、后台任务）。

    用法::

        async with get_db_session_ctx() as session:
            result = await session.execute(...)
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def close_engine() -> None:
    """关闭引擎，释放连接池（应用 shutdown 时调用）。"""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
