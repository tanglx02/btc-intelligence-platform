"""市场情绪 Provider。

- AlternativeMeProvider : 恐惧贪婪指数（完全免费，无需 Key）
- LunarCrushProvider    : 社交媒体情绪与热度（需 API Key）
"""

from app.providers.sentiment.alternative_me import AlternativeMeProvider
from app.providers.sentiment.lunar_crush import LunarCrushProvider

__all__ = [
    "AlternativeMeProvider",
    "LunarCrushProvider",
]
