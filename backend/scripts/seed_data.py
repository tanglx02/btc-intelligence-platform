"""种子数据脚本 — 初始化系统基础数据。

包含：
1. assets（BTC, USD, USDT）
2. providers（Binance, OKX, Coinbase, Glassnode, Coinglass, Farside, Deribit, FRED, Alternative.me）
3. indicator_definitions（核心技术/链上/情绪指标）
4. market_events（历史重大事件）

使用方式：
    cd backend
    python -m scripts.seed_data
"""

import asyncio
import sys
from pathlib import Path
from datetime import datetime, timezone

# 确保可以导入 app 模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session_factory, close_engine
from app.models.market import Asset
from app.models.provider import Provider
from app.models.indicator import IndicatorDefinition
from app.models.system import MarketEvent


# ======================== 种子数据定义 ========================

ASSETS = [
    {"symbol": "BTC", "name": "Bitcoin", "asset_type": "CRYPTO", "chain": "bitcoin", "decimals": 8},
    {"symbol": "USD", "name": "US Dollar", "asset_type": "FIAT", "chain": None, "decimals": 2},
    {"symbol": "USDT", "name": "Tether", "asset_type": "CRYPTO", "chain": "ethereum", "decimals": 6},
]

PROVIDERS = [
    {
        "name": "binance",
        "category": "MARKET",
        "base_url": "https://api.binance.com",
        "priority": 10,
        "rate_limit": 1200,
        "rate_limit_window": 60,
        "description": "全球最大加密货币交易所，提供实时行情、K线、深度数据",
        "supported_symbols": ["BTCUSDT", "BTC"],
        "supported_intervals": ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"],
    },
    {
        "name": "okx",
        "category": "MARKET",
        "base_url": "https://www.okx.com/api/v5",
        "priority": 20,
        "rate_limit": 600,
        "rate_limit_window": 60,
        "description": "全球领先数字资产交易平台，提供行情与衍生品数据",
        "supported_symbols": ["BTC-USDT", "BTC"],
        "supported_intervals": ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"],
    },
    {
        "name": "coinbase",
        "category": "MARKET",
        "base_url": "https://api.exchange.coinbase.com",
        "priority": 30,
        "rate_limit": 350,
        "rate_limit_window": 60,
        "description": "美国合规交易所，提供 BTC-USD 权威定价",
        "supported_symbols": ["BTC-USD", "BTC"],
        "supported_intervals": ["1m", "5m", "15m", "1h", "4h", "1d"],
    },
    {
        "name": "glassnode",
        "category": "ONCHAIN",
        "base_url": "https://api.glassnode.com/v1",
        "priority": 10,
        "rate_limit": 30,
        "rate_limit_window": 60,
        "description": "专业链上数据分析平台，提供活跃地址、SOPR、MVRV 等指标",
        "supported_symbols": ["BTC"],
    },
    {
        "name": "cryptoquant",
        "category": "ONCHAIN",
        "base_url": "https://api.cryptoquant.com/v1",
        "priority": 20,
        "rate_limit": 30,
        "rate_limit_window": 60,
        "description": "链上数据与交易所流量分析平台",
        "supported_symbols": ["BTC"],
    },
    {
        "name": "coinglass",
        "category": "DERIVATIVES",
        "base_url": "https://open-api.coinglass.com/public/v2",
        "priority": 10,
        "rate_limit": 60,
        "rate_limit_window": 60,
        "description": "衍生品聚合数据：资金费率、未平仓合约、清算数据",
        "supported_symbols": ["BTC"],
    },
    {
        "name": "deribit",
        "category": "OPTIONS",
        "base_url": "https://www.deribit.com/api/v2",
        "priority": 10,
        "rate_limit": 120,
        "rate_limit_window": 60,
        "description": "全球最大 BTC 期权交易所，提供期权链与隐含波动率",
        "supported_symbols": ["BTC"],
    },
    {
        "name": "farside",
        "category": "ETF",
        "base_url": "https://farside.co.uk",
        "priority": 10,
        "rate_limit": 10,
        "rate_limit_window": 60,
        "description": "ETF 资金流数据（每日更新）",
        "supported_symbols": ["BTC"],
    },
    {
        "name": "fred",
        "category": "MACRO",
        "base_url": "https://api.stlouisfed.org/fred",
        "priority": 10,
        "rate_limit": 120,
        "rate_limit_window": 60,
        "description": "美联储经济数据库，提供 CPI、利率、M2 等宏观指标",
        "supported_symbols": ["BTC"],
    },
    {
        "name": "alternative_me",
        "category": "SENTIMENT",
        "base_url": "https://api.alternative.me",
        "priority": 10,
        "rate_limit": 30,
        "rate_limit_window": 60,
        "description": "加密货币恐惧贪婪指数",
        "supported_symbols": ["BTC"],
    },
]

INDICATOR_DEFINITIONS = [
    # --- 技术指标 ---
    {
        "code": "RSI_14", "name": "Relative Strength Index (14)", "name_cn": "相对强弱指标(14)",
        "category": "TECHNICAL", "sub_category": "MOMENTUM",
        "description": "Measures speed and change of price movements, range 0-100",
        "description_cn": "衡量价格变动速度和幅度，范围 0-100",
        "formula": "RSI = 100 - 100/(1 + avg_gain/avg_loss)",
        "unit": "value", "value_range": "0-100", "frequency": "1d",
        "default_params": {"period": 14},
        "display_config": {"chart_type": "line", "zone": "oscillator", "overbought": 70, "oversold": 30},
        "interpretation": {"overbought": ">70", "oversold": "<30", "neutral": "30-70"},
        "data_dependencies": ["candles.close"],
        "is_primary": True, "sort_order": 1,
    },
    {
        "code": "MACD", "name": "Moving Average Convergence Divergence", "name_cn": "指数平滑异同移动平均线",
        "category": "TECHNICAL", "sub_category": "TREND",
        "description": "Trend-following momentum indicator showing relationship between two EMAs",
        "description_cn": "趋势跟踪动量指标，显示两条 EMA 之间的关系",
        "formula": "MACD = EMA(12) - EMA(26); Signal = EMA(MACD, 9)",
        "unit": "value", "frequency": "1d",
        "default_params": {"fast_period": 12, "slow_period": 26, "signal_period": 9},
        "display_config": {"chart_type": "histogram", "zone": "oscillator"},
        "data_dependencies": ["candles.close"],
        "is_primary": True, "sort_order": 2,
    },
    {
        "code": "BB_WIDTH", "name": "Bollinger Bands Width", "name_cn": "布林带宽度",
        "category": "TECHNICAL", "sub_category": "VOLATILITY",
        "description": "Measures volatility as percentage distance between upper and lower bands",
        "description_cn": "以百分比衡量上下轨之间的波动率",
        "formula": "BB_Width = (Upper - Lower) / Middle * 100",
        "unit": "percent", "frequency": "1d",
        "default_params": {"period": 20, "std_dev": 2},
        "display_config": {"chart_type": "line", "zone": "oscillator"},
        "data_dependencies": ["candles.close"],
        "is_primary": False, "sort_order": 3,
    },
    {
        "code": "MA_200", "name": "200-Day Moving Average", "name_cn": "200日均线",
        "category": "TECHNICAL", "sub_category": "TREND",
        "description": "Long-term trend indicator, price above = bullish",
        "description_cn": "长期趋势指标，价格在其上方为看涨",
        "formula": "MA200 = SMA(close, 200)",
        "unit": "USD", "frequency": "1d",
        "default_params": {"period": 200},
        "display_config": {"chart_type": "line", "zone": "overlay"},
        "data_dependencies": ["candles.close"],
        "is_primary": True, "sort_order": 4,
    },
    {
        "code": "VOLUME_MA_RATIO", "name": "Volume to MA Ratio", "name_cn": "成交量均线比",
        "category": "TECHNICAL", "sub_category": "VOLUME",
        "description": "Current volume relative to its moving average",
        "description_cn": "当前成交量相对于其均值的比率",
        "formula": "Ratio = Volume / SMA(Volume, 20)",
        "unit": "ratio", "frequency": "1d",
        "default_params": {"period": 20},
        "display_config": {"chart_type": "bar", "zone": "volume"},
        "data_dependencies": ["candles.volume"],
        "is_primary": False, "sort_order": 5,
    },
    # --- 链上指标 ---
    {
        "code": "MVRV", "name": "Market Value to Realized Value", "name_cn": "市值与已实现价值比",
        "category": "ONCHAIN", "sub_category": "VALUATION",
        "description": "Ratio of market cap to realized cap, identifies over/undervaluation",
        "description_cn": "市值与已实现市值的比率，识别高估/低估",
        "formula": "MVRV = Market Cap / Realized Cap",
        "unit": "ratio", "value_range": "0.5-5.0", "frequency": "1d",
        "default_params": {},
        "display_config": {"chart_type": "line", "zone": "panel"},
        "interpretation": {"overvalued": ">3.5", "undervalued": "<1.0", "fair": "1.0-3.5"},
        "data_dependencies": ["onchain_metrics.mvrv"],
        "is_primary": True, "sort_order": 10,
    },
    {
        "code": "SOPR", "name": "Spent Output Profit Ratio", "name_cn": "已花费输出利润率",
        "category": "ONCHAIN", "sub_category": "PROFITABILITY",
        "description": "Ratio of spent output value at current price vs creation price",
        "description_cn": "已花费输出在当前价格与创建价格之间的比率",
        "formula": "SOPR = Sum(spent_value) / Sum(creation_value)",
        "unit": "ratio", "value_range": "0.9-1.15", "frequency": "1d",
        "default_params": {},
        "display_config": {"chart_type": "line", "zone": "panel"},
        "interpretation": {"profit_taking": ">1.05", "capitulation": "<0.95", "neutral": "0.95-1.05"},
        "data_dependencies": ["onchain_metrics.sopr"],
        "is_primary": True, "sort_order": 11,
    },
    {
        "code": "NUPL", "name": "Net Unrealized Profit/Loss", "name_cn": "净未实现盈亏",
        "category": "ONCHAIN", "sub_category": "PROFITABILITY",
        "description": "Measures whether holders are in profit or loss",
        "description_cn": "衡量持有者整体处于盈利还是亏损状态",
        "formula": "NUPL = (Market Cap - Realized Cap) / Market Cap",
        "unit": "ratio", "value_range": "-0.5-1.0", "frequency": "1d",
        "default_params": {},
        "display_config": {"chart_type": "area", "zone": "panel"},
        "interpretation": {"euphoria": ">0.75", "belief": "0.5-0.75", "capitulation": "<0"},
        "data_dependencies": ["onchain_metrics.nupl"],
        "is_primary": True, "sort_order": 12,
    },
    {
        "code": "ACTIVE_ADDRESSES", "name": "Active Addresses", "name_cn": "活跃地址数",
        "category": "ONCHAIN", "sub_category": "NETWORK",
        "description": "Number of unique addresses active on the network",
        "description_cn": "网络上活跃的唯一地址数量",
        "formula": "Count(distinct addresses with transactions)",
        "unit": "count", "frequency": "1d",
        "default_params": {},
        "display_config": {"chart_type": "line", "zone": "panel"},
        "data_dependencies": ["address_metrics.active_addresses"],
        "is_primary": False, "sort_order": 13,
    },
    {
        "code": "EXCHANGE_NET_FLOW", "name": "Exchange Net Flow", "name_cn": "交易所净流量",
        "category": "ONCHAIN", "sub_category": "EXCHANGE_FLOW",
        "description": "Net BTC flowing into/out of exchanges; negative = bullish",
        "description_cn": "流入/流出交易所的 BTC 净值；负值为看涨",
        "formula": "Net Flow = Inflow - Outflow",
        "unit": "BTC", "frequency": "1d",
        "default_params": {},
        "display_config": {"chart_type": "bar", "zone": "panel"},
        "interpretation": {"bullish": "<-1000 BTC", "bearish": ">1000 BTC"},
        "data_dependencies": ["exchange_flows.net_flow"],
        "is_primary": True, "sort_order": 14,
    },
    # --- 衍生品指标 ---
    {
        "code": "FUNDING_RATE", "name": "Funding Rate", "name_cn": "资金费率",
        "category": "DERIVATIVES", "sub_category": "PERPETUAL",
        "description": "Periodic payment between long and short positions",
        "description_cn": "多空仓位之间的定期支付比率",
        "formula": "Funding Rate = (Mark - Index) / Index * clamp + interest",
        "unit": "percent", "value_range": "-0.1%-0.1%", "frequency": "8h",
        "default_params": {},
        "display_config": {"chart_type": "bar", "zone": "panel"},
        "interpretation": {"overleveraged_long": ">0.05%", "overleveraged_short": "<-0.05%"},
        "data_dependencies": ["derivatives.funding_rate"],
        "is_primary": True, "sort_order": 20,
    },
    {
        "code": "OPEN_INTEREST", "name": "Open Interest", "name_cn": "未平仓合约量",
        "category": "DERIVATIVES", "sub_category": "PERPETUAL",
        "description": "Total outstanding derivative contracts",
        "description_cn": "未平仓衍生品合约总量",
        "formula": "Sum(open positions)",
        "unit": "BTC", "frequency": "1h",
        "default_params": {},
        "display_config": {"chart_type": "line", "zone": "panel"},
        "data_dependencies": ["derivatives.open_interest"],
        "is_primary": False, "sort_order": 21,
    },
    {
        "code": "LONG_SHORT_RATIO", "name": "Long/Short Ratio", "name_cn": "多空比",
        "category": "DERIVATIVES", "sub_category": "POSITIONING",
        "description": "Ratio of long to short positions among top traders",
        "description_cn": "大户多空持仓比率",
        "formula": "Long Accounts / Short Accounts",
        "unit": "ratio", "frequency": "1h",
        "default_params": {},
        "display_config": {"chart_type": "line", "zone": "panel"},
        "interpretation": {"extreme_long": ">3.0", "extreme_short": "<0.5"},
        "data_dependencies": ["derivatives.long_short_ratio"],
        "is_primary": False, "sort_order": 22,
    },
    # --- 情绪指标 ---
    {
        "code": "FEAR_GREED", "name": "Fear & Greed Index", "name_cn": "恐惧贪婪指数",
        "category": "SENTIMENT", "sub_category": "INDEX",
        "description": "Composite sentiment index from multiple sources, range 0-100",
        "description_cn": "多来源综合情绪指数，范围 0-100",
        "formula": "Weighted avg(volatility, momentum, social, dominance, trends)",
        "unit": "value", "value_range": "0-100", "frequency": "1d",
        "default_params": {},
        "display_config": {"chart_type": "gauge", "zone": "panel"},
        "interpretation": {"extreme_greed": ">75", "greed": "55-75", "neutral": "45-55", "fear": "25-45", "extreme_fear": "<25"},
        "data_dependencies": ["sentiment.fear_greed"],
        "is_primary": True, "sort_order": 30,
    },
    # --- 宏观指标 ---
    {
        "code": "REAL_RATE", "name": "Real Interest Rate", "name_cn": "实际利率",
        "category": "MACRO", "sub_category": "RATES",
        "description": "Federal funds rate minus core CPI inflation",
        "description_cn": "联邦基金利率减去核心 CPI 通胀率",
        "formula": "Real Rate = Fed Funds Rate - Core CPI YoY",
        "unit": "percent", "frequency": "1M",
        "default_params": {},
        "display_config": {"chart_type": "line", "zone": "panel"},
        "interpretation": {"tight": ">2%", "neutral": "0-2%", "loose": "<0%"},
        "data_dependencies": ["macro_series.FEDFUNDS", "macro_series.CPICSLFE"],
        "is_primary": True, "sort_order": 40,
    },
    {
        "code": "M2_YOY", "name": "M2 Money Supply YoY", "name_cn": "M2 货币供应量同比",
        "category": "MACRO", "sub_category": "LIQUIDITY",
        "description": "Year-over-year growth rate of M2 money supply",
        "description_cn": "M2 货币供应量年增长率",
        "formula": "M2_YoY = (M2_current - M2_year_ago) / M2_year_ago * 100",
        "unit": "percent", "frequency": "1M",
        "default_params": {},
        "display_config": {"chart_type": "line", "zone": "panel"},
        "data_dependencies": ["macro_series.M2SL"],
        "is_primary": False, "sort_order": 41,
    },
    {
        "code": "DXY", "name": "US Dollar Index", "name_cn": "美元指数",
        "category": "MACRO", "sub_category": "CURRENCY",
        "description": "Value of USD relative to a basket of foreign currencies",
        "description_cn": "美元相对于一篮子外币的价值",
        "formula": "Geometric mean of exchange rates vs 6 currencies",
        "unit": "index", "frequency": "1d",
        "default_params": {},
        "display_config": {"chart_type": "line", "zone": "panel"},
        "interpretation": {"strong_dollar": ">105", "weak_dollar": "<95"},
        "data_dependencies": ["macro_series.DTWEEXBGS"],
        "is_primary": False, "sort_order": 42,
    },
    # --- ETF 指标 ---
    {
        "code": "ETF_NET_FLOW", "name": "Spot ETF Net Flow", "name_cn": "现货 ETF 净流量",
        "category": "ETF", "sub_category": "FLOWS",
        "description": "Aggregate daily net flow across all US spot BTC ETFs",
        "description_cn": "所有美国现货 BTC ETF 的每日净流量汇总",
        "formula": "Sum(daily_flow) across all ETFs",
        "unit": "USD", "frequency": "1d",
        "default_params": {},
        "display_config": {"chart_type": "bar", "zone": "panel"},
        "data_dependencies": ["etf_flows.daily_flow"],
        "is_primary": True, "sort_order": 50,
    },
]

MARKET_EVENTS = [
    {
        "event_category": "CRYPTO", "event_type": "HALVING",
        "title": "Bitcoin 2nd Halving", "title_cn": "比特币第二次减半",
        "description": "Block reward reduced from 25 BTC to 12.5 BTC at block 420,000",
        "description_cn": "区块奖励从 25 BTC 降至 12.5 BTC（区块高度 420,000）",
        "significance": 0.9, "is_major": True,
        "event_time": datetime(2016, 7, 9, tzinfo=timezone.utc),
        "observation_time": datetime(2016, 7, 9, tzinfo=timezone.utc),
        "btc_price_at_event": 650.00,
        "tags": ["halving", "supply"],
    },
    {
        "event_category": "MARKET_STRUCTURE", "event_type": "ATH",
        "title": "BTC ATH $19,783", "title_cn": "比特币历史高点 $19,783",
        "description": "Bitcoin reached all-time high during the 2017 bull run peak",
        "description_cn": "比特币在 2017 年牛市顶峰达到历史高点",
        "significance": 1.0, "is_major": True,
        "event_time": datetime(2017, 12, 17, tzinfo=timezone.utc),
        "observation_time": datetime(2017, 12, 17, tzinfo=timezone.utc),
        "btc_price_at_event": 19783.00,
        "tags": ["ath", "bubble", "peak"],
    },
    {
        "event_category": "MARKET_STRUCTURE", "event_type": "CRASH",
        "title": "Black Thursday", "title_cn": "黑色星期四",
        "description": "BTC crashed ~50% in 24h amid COVID-19 panic selling",
        "description_cn": "新冠疫情恐慌性抛售中 BTC 24 小时内暴跌约 50%",
        "significance": 0.95, "is_major": True,
        "event_time": datetime(2020, 3, 12, tzinfo=timezone.utc),
        "observation_time": datetime(2020, 3, 12, tzinfo=timezone.utc),
        "btc_price_at_event": 3800.00, "btc_price_before_24h": 7900.00,
        "market_impact": {"price_change_pct": -51.9, "volume_spike": True},
        "tags": ["crash", "covid", "liquidation"],
    },
    {
        "event_category": "CRYPTO", "event_type": "HALVING",
        "title": "Bitcoin 3rd Halving", "title_cn": "比特币第三次减半",
        "description": "Block reward reduced from 12.5 BTC to 6.25 BTC at block 630,000",
        "description_cn": "区块奖励从 12.5 BTC 降至 6.25 BTC（区块高度 630,000）",
        "significance": 0.9, "is_major": True,
        "event_time": datetime(2020, 5, 11, tzinfo=timezone.utc),
        "observation_time": datetime(2020, 5, 11, tzinfo=timezone.utc),
        "btc_price_at_event": 8700.00,
        "tags": ["halving", "supply"],
    },
    {
        "event_category": "MARKET_STRUCTURE", "event_type": "ATH",
        "title": "BTC ATH $64,863", "title_cn": "比特币历史高点 $64,863",
        "description": "Bitcoin reached new ATH on Coinbase IPO day",
        "description_cn": "Coinbase 上市当天比特币创新高",
        "significance": 0.9, "is_major": True,
        "event_time": datetime(2021, 4, 14, tzinfo=timezone.utc),
        "observation_time": datetime(2021, 4, 14, tzinfo=timezone.utc),
        "btc_price_at_event": 64863.00,
        "tags": ["ath", "coinbase", "ipo"],
    },
    {
        "event_category": "MARKET_STRUCTURE", "event_type": "ATH",
        "title": "BTC ATH $68,789", "title_cn": "比特币历史高点 $68,789",
        "description": "Bitcoin reached cycle top before the 2022 bear market",
        "description_cn": "比特币在 2022 年熊市前达到周期顶部",
        "significance": 1.0, "is_major": True,
        "event_time": datetime(2021, 11, 10, tzinfo=timezone.utc),
        "observation_time": datetime(2021, 11, 10, tzinfo=timezone.utc),
        "btc_price_at_event": 68789.00,
        "tags": ["ath", "cycle_top"],
    },
    {
        "event_category": "MARKET_STRUCTURE", "event_type": "CRASH",
        "title": "Terra/LUNA Collapse", "title_cn": "Terra/LUNA 崩盘",
        "description": "Algorithmic stablecoin UST depegged, LUNA went to near-zero, $40B+ wiped out",
        "description_cn": "算法稳定币 UST 脱锚，LUNA 归零，超过 400 亿美元蒸发",
        "significance": 0.85, "is_major": True,
        "event_time": datetime(2022, 5, 9, tzinfo=timezone.utc),
        "observation_time": datetime(2022, 5, 9, tzinfo=timezone.utc),
        "btc_price_at_event": 30000.00, "btc_price_before_24h": 34000.00,
        "market_impact": {"price_change_pct": -11.8},
        "tags": ["crash", "terra", "stablecoin"],
    },
    {
        "event_category": "MARKET_STRUCTURE", "event_type": "CRASH",
        "title": "FTX Collapse", "title_cn": "FTX 崩盘",
        "description": "FTX exchange filed bankruptcy, $8B user funds missing",
        "description_cn": "FTX 交易所申请破产，80 亿美元用户资金失踪",
        "significance": 0.9, "is_major": True,
        "event_time": datetime(2022, 11, 11, tzinfo=timezone.utc),
        "observation_time": datetime(2022, 11, 11, tzinfo=timezone.utc),
        "btc_price_at_event": 16500.00, "btc_price_before_24h": 21000.00,
        "market_impact": {"price_change_pct": -21.4},
        "tags": ["crash", "ftx", "exchange", "bankruptcy"],
    },
    {
        "event_category": "REGULATORY", "event_type": "ETF_LAUNCH",
        "title": "US Spot BTC ETF Approved", "title_cn": "美国现货比特币 ETF 获批",
        "description": "SEC approved 11 spot Bitcoin ETFs including BlackRock iShares and Fidelity",
        "description_cn": "SEC 批准 11 只现货比特币 ETF，包括贝莱德 iShares 和富达",
        "significance": 0.95, "is_major": True,
        "event_time": datetime(2024, 1, 10, tzinfo=timezone.utc),
        "observation_time": datetime(2024, 1, 10, tzinfo=timezone.utc),
        "btc_price_at_event": 46000.00,
        "tags": ["etf", "sec", "regulatory", "institutional"],
    },
    {
        "event_category": "CRYPTO", "event_type": "HALVING",
        "title": "Bitcoin 4th Halving", "title_cn": "比特币第四次减半",
        "description": "Block reward reduced from 6.25 BTC to 3.125 BTC at block 840,000",
        "description_cn": "区块奖励从 6.25 BTC 降至 3.125 BTC（区块高度 840,000）",
        "significance": 0.9, "is_major": True,
        "event_time": datetime(2024, 4, 20, tzinfo=timezone.utc),
        "observation_time": datetime(2024, 4, 20, tzinfo=timezone.utc),
        "btc_price_at_event": 64000.00,
        "tags": ["halving", "supply"],
    },
]


# ======================== 执行逻辑 ========================

async def seed_assets(session: AsyncSession) -> None:
    """插入资产数据。"""
    for asset_data in ASSETS:
        result = await session.execute(select(Asset).where(Asset.symbol == asset_data["symbol"]))
        if result.scalar_one_or_none() is None:
            session.add(Asset(**asset_data))
    await session.flush()
    print(f"  [assets] 已插入/跳过 {len(ASSETS)} 条记录")


async def seed_providers(session: AsyncSession) -> None:
    """插入 Provider 数据源配置。"""
    for provider_data in PROVIDERS:
        result = await session.execute(select(Provider).where(Provider.name == provider_data["name"]))
        if result.scalar_one_or_none() is None:
            session.add(Provider(**provider_data))
    await session.flush()
    print(f"  [providers] 已插入/跳过 {len(PROVIDERS)} 条记录")


async def seed_indicator_definitions(session: AsyncSession) -> None:
    """插入指标定义。"""
    for ind_data in INDICATOR_DEFINITIONS:
        result = await session.execute(
            select(IndicatorDefinition).where(IndicatorDefinition.code == ind_data["code"])
        )
        if result.scalar_one_or_none() is None:
            session.add(IndicatorDefinition(**ind_data))
    await session.flush()
    print(f"  [indicator_definitions] 已插入/跳过 {len(INDICATOR_DEFINITIONS)} 条记录")


async def seed_market_events(session: AsyncSession) -> None:
    """插入历史市场事件。"""
    inserted = 0
    for event_data in MARKET_EVENTS:
        # 通过 title + event_time 判重
        result = await session.execute(
            select(MarketEvent).where(
                MarketEvent.title == event_data["title"],
                MarketEvent.event_time == event_data["event_time"],
            )
        )
        if result.scalar_one_or_none() is None:
            session.add(MarketEvent(**event_data))
            inserted += 1
    await session.flush()
    print(f"  [market_events] 已插入 {inserted} 条，跳过 {len(MARKET_EVENTS) - inserted} 条")


async def run_seed() -> None:
    """主入口：执行全部种子数据插入。"""
    print("=" * 60)
    print("BTC 全市场智能研究平台 — 种子数据初始化")
    print("=" * 60)

    session_factory = get_session_factory()
    async with session_factory() as session:
        async with session.begin():
            print("\n[1/4] 插入资产数据...")
            await seed_assets(session)

            print("[2/4] 插入 Provider 配置...")
            await seed_providers(session)

            print("[3/4] 插入指标定义...")
            await seed_indicator_definitions(session)

            print("[4/4] 插入历史市场事件...")
            await seed_market_events(session)

    print("\n" + "=" * 60)
    print("种子数据初始化完成！")
    print("=" * 60)

    await close_engine()


if __name__ == "__main__":
    asyncio.run(run_seed())
