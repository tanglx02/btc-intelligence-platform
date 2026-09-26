"""交易对 Symbol 标准化映射器。

系统内部统一使用 ``BASE/QUOTE`` 规范格式（如 ``"BTC/USDT"``），
与 BaseMarketProvider 抽象签名及 config/providers.yaml 中
supported_symbols 的约定保持一致。

各交易所格式差异：

===========  =====================  ================================
交易所        格式                   示例
===========  =====================  ================================
Binance      BASEQUOTE              BTCUSDT
OKX          BASE-QUOTE             BTC-USDT（永续为 BTC-USDT-SWAP）
Coinbase     BASE-QUOTE             BTC-USD（Exchange 无 BTC-USDT 现货）
Bybit        BASEQUOTE              BTCUSDT
Kraken       BASEQUOTE（BTC→XBT）    XBTUSD / XXBTZUSD（标准代码）
===========  =====================  ================================

对外（API/DB）另提供 ``to_compact`` 的 "BTCUSDT" 压缩格式，
与 app.models.market 表默认值对齐。
"""

import re

# 常见稳定币/计价资产别名归一化（交易所不提供该计价对时回退）
_QUOTE_FALLBACK: dict[str, str] = {
    "USDT": "USD",
    "USDC": "USD",
}

# 反向回退（交易所只提供 USDT 计价时）
_QUOTE_FALLBACK_USDT: dict[str, str] = {
    "USD": "USDT",
}

# Kraken 资产代码映射（Kraken 用 XBT 表示 BTC）
_KRAKEN_ASSET_MAP: dict[str, str] = {
    "BTC": "XBT",
}
_KRAKEN_ASSET_REVERSE: dict[str, str] = {
    "XBT": "BTC",
}

# Kraken 标准代码前缀（老式 pair 写法，如 XXBTZUSD）
_KRAKEN_PREFIXED: dict[str, str] = {
    "XBT": "XXBT",
    "ETH": "XETH",
    "USD": "ZUSD",
    "EUR": "ZEUR",
    "USDT": "USDT",
}
# 反向：标准代码前缀 -> 原始资产（XXBT -> XBT / ZUSD -> USD）
_KRAKEN_PREFIXED_REVERSE: dict[str, str] = {v: k for k, v in _KRAKEN_PREFIXED.items()}


def normalize_symbol(symbol: str) -> str:
    """将任意格式的交易对归一化为内部规范格式 ``BASE/QUOTE``。

    支持输入：``BTCUSDT`` / ``BTC-USDT`` / ``BTC/USDT`` / ``btcusdt`` /
    ``XBTUSD`` / ``XXBTZUSD``。

    Args:
        symbol: 任意格式交易对字符串

    Returns:
        规范格式字符串，如 "BTC/USDT"

    Raises:
        ValueError: 无法识别的 symbol 格式
    """
    if not symbol or not isinstance(symbol, str):
        raise ValueError(f"无效的交易对: {symbol!r}")

    s = symbol.strip().upper()

    # 已含分隔符：BTC/USDT 或 BTC-USDT 或 BTC-USDT-SWAP
    if "/" in s or "-" in s:
        parts = [p for p in re.split(r"[/-]", s) if p]
        if len(parts) < 2:
            raise ValueError(f"无效的交易对格式: {symbol!r}")
        base, quote = parts[0], parts[1]
        base = _KRAKEN_ASSET_REVERSE.get(base, base)
        return f"{base}/{quote}"

    # Kraken 标准代码：XXBTZUSD / XBTZUSD（带 Z 前缀的计价资产）
    raw_base: str
    quote: str
    for z_pos in (5, 4):
        if len(s) >= z_pos + 4 and s[z_pos] == "Z":
            raw_base = s[:z_pos]
            quote = s[z_pos + 1:]
            break
    else:
        # 压缩格式：BTCUSDT / XBTUSD / BTCUSD
        raw_base = s[:3]
        quote = s[3:]

    # 逐层剥离 Kraken 前缀（XXBT -> XBT）与资产别名（XBT -> BTC）
    base = _KRAKEN_PREFIXED_REVERSE.get(raw_base, raw_base)
    base = _KRAKEN_ASSET_REVERSE.get(base, base)
    quote = _KRAKEN_PREFIXED_REVERSE.get(quote, quote)
    quote = _KRAKEN_ASSET_REVERSE.get(quote, quote)
    if len(quote) < 3:
        raise ValueError(f"无效的交易对格式: {symbol!r}")
    return f"{base}/{quote}"


def split_symbol(symbol: str) -> tuple[str, str]:
    """拆分交易对为 (base, quote)。输入任意格式。"""
    normalized = normalize_symbol(symbol)
    base, quote = normalized.split("/", 1)
    return base, quote


def to_compact(symbol: str) -> str:
    """转为压缩格式 "BTCUSDT"（API/DB 存储格式）。"""
    base, quote = split_symbol(symbol)
    return f"{base}{quote}"


# ---- 各交易所格式转换 ----


def to_binance(symbol: str) -> str:
    """内部格式 -> Binance 格式（BTCUSDT）。"""
    base, quote = split_symbol(symbol)
    return f"{base}{quote}"


def to_okx(symbol: str, *, swap: bool = False) -> str:
    """内部格式 -> OKX instId（BTC-USDT / BTC-USDT-SWAP）。"""
    base, quote = split_symbol(symbol)
    inst_id = f"{base}-{quote}"
    return f"{inst_id}-SWAP" if swap else inst_id


def to_coinbase(symbol: str, *, fallback_map: dict[str, str] | None = None) -> str:
    """内部格式 -> Coinbase product_id（BTC-USD）。

    Coinbase Exchange 现货以 USD 计价为主，USDT 计价对缺失时
    按 fallback_map 回退（默认 USDT/USDC -> USD）。
    """
    base, quote = split_symbol(symbol)
    mapping = fallback_map if fallback_map is not None else _QUOTE_FALLBACK
    quote = mapping.get(quote, quote)
    return f"{base}-{quote}"


def to_bybit(symbol: str) -> str:
    """内部格式 -> Bybit 格式（BTCUSDT）。"""
    base, quote = split_symbol(symbol)
    return f"{base}{quote}"


def to_kraken(symbol: str, *, standard_code: bool = False) -> str:
    """内部格式 -> Kraken pair（XBTUSD 或 XXBTZUSD）。

    Args:
        symbol: 任意格式交易对
        standard_code: True 时返回老式标准代码（XXBTZUSD）
    """
    base, quote = split_symbol(symbol)
    base = _KRAKEN_ASSET_MAP.get(base, base)
    if standard_code:
        base = _KRAKEN_PREFIXED.get(base, base)
        quote = _KRAKEN_PREFIXED.get(quote, quote)
    return f"{base}{quote}"


def from_kraken_pair(pair: str) -> str:
    """Kraken 响应中的 pair key -> 内部规范格式。

    Kraken 响应的 key 可能是 "XBTUSD" / "XXBTZUSD" / "XXBTZUSD" 的变体。
    """
    return normalize_symbol(pair)


def to_okx_quote_fallback(symbol: str) -> str:
    """OKX 无 USD 计价对时回退为 USDT（如 BTC/USD -> BTC-USDT）。"""
    base, quote = split_symbol(symbol)
    quote = _QUOTE_FALLBACK_USDT.get(quote, quote)
    return f"{base}-{quote}"


__all__ = [
    "from_kraken_pair",
    "normalize_symbol",
    "split_symbol",
    "to_binance",
    "to_bybit",
    "to_coinbase",
    "to_compact",
    "to_kraken",
    "to_okx",
    "to_okx_quote_fallback",
]
