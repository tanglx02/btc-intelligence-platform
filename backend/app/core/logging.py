"""日志配置（基于 loguru）。

提供统一的日志初始化入口，输出到控制台并按天滚动写入文件。
生产环境应通过环境变量控制日志级别与格式（JSON）。

占位骨架：具体的 sink 配置、第三方库日志拦截将在后续迭代补充。
"""

import sys

from loguru import logger

from app.core.config import settings


def setup_logging() -> None:
    """初始化全局日志配置。

    - 移除默认 handler
    - 控制台输出（开发环境彩色，生产环境可切换 JSON）
    - 按天滚动的文件输出
    """
    logger.remove()

    level = "DEBUG" if settings.debug else "INFO"

    logger.add(
        sys.stderr,
        level=level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
        colorize=True,
    )

    logger.add(
        "logs/app_{time:YYYY-MM-DD}.log",
        level=level,
        rotation="00:00",
        retention="30 days",
        encoding="utf-8",
        enqueue=True,
    )


__all__ = ["logger", "setup_logging"]
