"""宏观经济 Provider。

- FREDProvider      : 美联储经济数据库（利率/美元指数/通胀/就业/流动性，需免费 API Key）
- WorldBankProvider : 世界银行公开数据（GDP/通胀/失业率/M2，年度，无需 Key）
"""

from app.providers.macro.fred import FREDProvider
from app.providers.macro.world_bank import WorldBankProvider

__all__ = [
    "FREDProvider",
    "WorldBankProvider",
]
