"""ETF 资金流 Provider。

- FarsideProvider   : Farside Investors 每日 ETF 流量（网页抓取，免费）
- SoSoValueProvider : SoSoValue ETF 流量与持仓（需 API Key）
"""

from app.providers.etf.farside import FarsideProvider
from app.providers.etf.sosovalue import SoSoValueProvider

__all__ = [
    "FarsideProvider",
    "SoSoValueProvider",
]
