"""独立调度器进程入口。

生产部署中 Scheduler 与 API 进程分离运行（见 docker-compose.prod.yml 的
scheduler 服务），避免长任务阻塞 API worker。本入口启动全局调度器并保持
运行，接收 SIGTERM / SIGINT 优雅停止（容器 stop 时由 Docker 发送 SIGTERM）。

用法::

    python -m app.scheduler.worker
"""

import asyncio
import signal

from loguru import logger

from app.core.logging import setup_logging


async def main() -> None:
    """启动调度器并阻塞等待退出信号。"""
    setup_logging()
    # 延迟导入：确保日志配置先于调度器任务注册生效
    from app.scheduler import get_scheduler

    scheduler = get_scheduler()
    await scheduler.start()
    logger.info("Scheduler worker 已启动（等待退出信号...）")

    # AlertEngine：与调度器同进程专职运行（生产环境 API 进程已通过
    # ALERT_ENABLED=false 禁用告警，避免多 worker 双发）
    alert_engine = None
    try:
        from app.alerts import get_alert_engine

        alert_engine = get_alert_engine()
        if alert_engine is not None:
            await alert_engine.start()
            logger.info("Alert engine started (scheduler worker)")
    except ImportError:
        logger.info("Alert engine module not available")
    except Exception as e:  # noqa: BLE001 - 可选模块启动失败不阻断调度进程
        logger.error(f"Alert engine startup failed: {e}")
        alert_engine = None

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop(sig_name: str) -> None:
        logger.info("收到信号 {}，开始优雅停止调度器", sig_name)
        stop_event.set()

    def _signal_handler_fallback(signum, frame) -> None:  # noqa: ARG001
        # Windows / 部分事件循环不支持 loop.add_signal_handler 时的后备
        stop_event.set()

    for sig, name in ((signal.SIGINT, "SIGINT"), (signal.SIGTERM, "SIGTERM")):
        try:
            loop.add_signal_handler(sig, _request_stop, name)
        except NotImplementedError:
            signal.signal(sig, _signal_handler_fallback)

    try:
        await stop_event.wait()
    finally:
        if alert_engine is not None:
            try:
                await alert_engine.stop()
                logger.info("Alert engine stopped")
            except Exception as e:  # noqa: BLE001 - 关闭阶段任何异常都不应阻断
                logger.warning(f"Alert engine stop failed: {e}")
        await scheduler.stop()
        logger.info("Scheduler worker 已退出")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # Ctrl+C 双保险（fallback 信号路径下可能出现）
        pass
