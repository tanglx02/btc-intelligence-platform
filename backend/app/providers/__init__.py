"""数据源适配层。

每个子模块对应一类外部数据源，统一继承 base 中的抽象适配器接口，
便于在 service 层以一致的方式调用与降级：

- base         : 适配器抽象基类与通用重试/限流逻辑
- market       : 行情数据（价格、成交量、K线）
- onchain      : 链上数据（活跃地址、转账量、交易所净流入等）
- etf          : 现货 ETF 资金流数据
- derivatives  : 衍生品数据（资金费率、未平仓合约、多空比）
- options      : 期权数据（隐含波动率、看跌/看涨比、最大痛点）
- macro        : 宏观经济数据（利率、美元指数、流动性）
- sentiment    : 市场情绪数据（恐惧贪婪指数、社交热度）

核心运行时组件：

- ProviderRegistry : Provider 注册中心（单例）
- ProviderManager  : 优先级队列 + 请求路由 + Failover
- HealthMonitor    : 健康检查 + 8 维度评分 + 状态机
- FailoverEngine   : 自动故障切换 + 恢复探测 + 防抖动
- HTTPClient       : httpx 异步客户端封装
"""

from app.providers.base import (
    BaseProvider,
    ProviderConfig,
    ProviderRegistry,
)
from app.providers.failover import FailoverEngine
from app.providers.health_monitor import HealthMonitor
from app.providers.manager import PrioritizedProvider, ProviderManager
from app.providers.network import HTTPClient, NetworkErrorClassifier
from app.providers.rate_limiter import (
    RateLimiterFactory,
    SlidingWindowRateLimiter,
    TokenBucketRateLimiter,
)

__all__ = [
    # base
    "BaseProvider",
    "ProviderConfig",
    "ProviderRegistry",
    # network
    "HTTPClient",
    "NetworkErrorClassifier",
    # rate limiter
    "RateLimiterFactory",
    "SlidingWindowRateLimiter",
    "TokenBucketRateLimiter",
    # runtime components
    "PrioritizedProvider",
    "ProviderManager",
    "HealthMonitor",
    "FailoverEngine",
]
