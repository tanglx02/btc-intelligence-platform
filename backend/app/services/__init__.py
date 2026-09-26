"""业务服务层。

封装 Provider 抽象层，向上层（API / Scheduler）提供统一的数据获取入口：

- ProviderService : Provider 业务层统一入口（缓存 + 标准化 + Failover 封装）
- ServiceResult   : 业务层统一返回结构（data + metadata + quality_status）
"""

from app.services.provider_service import (
    ProviderService,
    ServiceResult,
    get_provider_service,
    set_provider_service,
)

__all__ = [
    "ProviderService",
    "ServiceResult",
    "get_provider_service",
    "set_provider_service",
]
