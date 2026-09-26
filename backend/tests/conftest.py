"""pytest 全局配置与共享 fixtures（占位骨架）。

后续将在此提供：
- 异步事件循环配置
- 测试数据库会话 / 事务回滚 fixture
- FastAPI TestClient fixture
- 数据源 mock fixture
"""

import pytest


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """指定 anyio 使用的异步后端。"""
    return "asyncio"
