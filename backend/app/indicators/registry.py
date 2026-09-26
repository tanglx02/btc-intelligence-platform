"""指标注册中心。

职责
----
- **自动发现**：从 ``technical`` / ``onchain`` / ``derivatives`` / ``composite``
  四个子包的 ``INDICATOR_CLASSES`` 列表收集全部指标类并注册；
- **实例复用**：以指标 ``name`` 为键缓存单例，避免重复实例化；
- **查询接口**：``get_indicator`` / ``list_indicators`` / ``names``；
- **双模式解释**：``get_indicator_definition`` 返回「普通用户解释 + 专业模式详情」，
  与 ``indicator_definitions`` 表结构对齐（见指标字典 §3）。

元数据以本模块的 :data:`INDICATOR_DEFINITIONS` 为唯一权威来源（Single Source of Truth），
落库脚本应据此写入 ``indicator_definitions`` 表。
"""

# 本文件的 INDICATOR_DEFINITIONS 为密集的中文元数据表（普通解释 + 专业详情），
# 描述性长行不宜强行折行，故对本文件豁免 E501（行宽）检查。
# ruff: noqa: E501

from __future__ import annotations

from typing import Any

from .base import IndicatorBase, IndicatorCategory

__all__ = [
    "IndicatorRegistry",
    "registry",
    "INDICATOR_DEFINITIONS",
    "get_indicator",
    "list_indicators",
    "get_indicator_definition",
]


# --------------------------------------------------------------------------- #
# 指标元数据字典（普通解释 + 专业详情），与 indicator_definitions 表对齐
# --------------------------------------------------------------------------- #
INDICATOR_DEFINITIONS: dict[str, dict[str, Any]] = {
    # ---- 技术指标 ----
    "tech.sma": {
        "name_en": "Simple Moving Average", "unit": "usd", "value_range": ">0",
        "formula": "SMA(N) = Σ(Close_i, i=1..N) / N",
        "percentile_method": "ROLLING_WINDOW",
        "description_simple": "过去 N 天市场的平均买入成本线，价格在其上方说明近期买入者整体赚钱。",
        "description_pro": "简单移动平均，对窗口内每个收盘价等权平均；常配合 price/MA−1 乖离率与金叉死叉使用。",
        "historical_meaning": "价格站上 200DMA 多数时间对应牛市，跌破对应熊市。",
        "notes": "365DMA 需至少 1 年数据；横盘期均线信号意义弱。",
        "consumed_by": ["Cycle Engine", "Risk Engine", "行情页"],
        "data_dependencies": ["candles(close)"], "output_columns": ["value"],
    },
    "tech.ema": {
        "name_en": "Exponential Moving Average", "unit": "usd", "value_range": ">0",
        "formula": "EMA_t = Close_t × k + EMA_{t-1} × (1−k), k = 2/(N+1)",
        "percentile_method": "ROLLING_WINDOW",
        "description_simple": "对近期价格更敏感的平均成本线，反应比简单均线更快。",
        "description_pro": "指数加权移动平均，平滑因子 k=2/(N+1)；MACD 即由 EMA(12)−EMA(26) 派生。",
        "historical_meaning": "EMA 比 SMA 更早反映趋势转折，但噪音也更多。",
        "notes": "小时线常用 {25,99,200}。", "consumed_by": ["Cycle Engine", "行情页"],
        "data_dependencies": ["candles(close)"], "output_columns": ["value"],
    },
    "tech.wma": {
        "name_en": "Weighted Moving Average", "unit": "usd", "value_range": ">0",
        "formula": "WMA(N) = Σ(Close_{N-i} × (N-i)) / Σ(1..N)",
        "percentile_method": "ROLLING_WINDOW",
        "description_simple": "线性加权的平均成本线，敏感度介于 SMA 与 EMA 之间。",
        "description_pro": "窗口内最新样本权重 N、最旧权重 1，分母为 N(N+1)/2。",
        "historical_meaning": "偏向线性加权，趋势跟随噪音低于 EMA。",
        "notes": "需至少 N 期数据。", "consumed_by": ["行情页"],
        "data_dependencies": ["candles(close)"], "output_columns": ["value"],
    },
    "tech.rsi": {
        "name_en": "Relative Strength Index", "unit": "index", "value_range": "0~100",
        "formula": "RSI = 100 − 100/(1+RS), RS = AvgGain(N)/AvgLoss(N)（Wilder 平滑）",
        "percentile_method": "ROLLING_WINDOW",
        "description_simple": "衡量最近一段时间买方与卖方谁更有力气，越高越可能短期过热。",
        "description_pro": "AvgGain/AvgLoss 采用 Wilder 平滑（首值 N 期简单平均，其后递推）；N∈{14,21}。",
        "historical_meaning": ">70 常见于上涨加速与局部顶部；<30 常见于恐慌底部。",
        "notes": "单边趋势中 RSI 会长期钝化，禁止单独作为反转依据；须标注周期。",
        "consumed_by": ["Cycle Engine", "Risk Engine", "情绪面板"],
        "data_dependencies": ["candles(close)"], "output_columns": ["value"],
    },
    "tech.macd": {
        "name_en": "Moving Average Convergence Divergence", "unit": "usd", "value_range": "(-∞,∞)",
        "formula": "DIF = EMA(12)−EMA(26); DEA = EMA(DIF,9); Histogram = DIF−DEA",
        "percentile_method": "ROLLING_WINDOW",
        "description_simple": "观察短期与长期成本差距是在拉大还是缩小，判断涨跌动能强弱。",
        "description_pro": "输出 macd_line/ signal_line/ histogram 三列，可检测金叉死叉与背离。",
        "historical_meaning": "零轴上方金叉多对应上升趋势中继；Histogram 收敛预示动能减弱。",
        "notes": "滞后指标，横盘期噪音多，应结合 ADX 过滤。",
        "consumed_by": ["Cycle Engine", "行情页"],
        "data_dependencies": ["candles(close)"], "output_columns": ["macd_line", "signal_line", "histogram"],
    },
    "tech.adx": {
        "name_en": "Average Directional Index", "unit": "index", "value_range": "0~100",
        "formula": "DX = 100×|+DI−−DI|/(+DI+−DI); ADX = Wilder(DX, N)",
        "percentile_method": "ROLLING_WINDOW",
        "description_simple": "衡量市场是有明确方向的趋势市还是来回震荡的盘整市，不区分涨跌。",
        "description_pro": "输出 adx/ plus_di/ minus_di；ADX>25 且 +DI>−DI 通常对应健康上升趋势。",
        "historical_meaning": "ADX<20 通常为震荡市，此时 MA/MACD 信号可靠性显著下降。",
        "notes": "只用于趋势强度判断，作为其他趋势因子的置信度调节器。",
        "consumed_by": ["Cycle Engine", "Risk Engine"],
        "data_dependencies": ["candles(high, low, close)"], "output_columns": ["adx", "plus_di", "minus_di"],
    },
    "tech.atr": {
        "name_en": "Average True Range", "unit": "usd", "value_range": ">=0",
        "formula": "TR = max(H−L, |H−C_prev|, |L−C_prev|); ATR = Wilder(TR, N); ATR% = ATR/Close×100",
        "percentile_method": "ROLLING_WINDOW",
        "description_simple": "最近两周市场平均每天上下波动多少，衡量市场颠簸程度，越大短期风险越高。",
        "description_pro": "输出 value（ATR，USD）与 atr_percent（ATR%）；跨期比较必须用 ATR%。",
        "historical_meaning": "ATR% 骤升常对应恐慌或逼空；长期低 ATR% 后的突破常伴随趋势启动。",
        "notes": "只度量波动幅度不度量方向。", "consumed_by": ["Risk Engine", "Backtest", "定投模拟"],
        "data_dependencies": ["candles(high, low, close)"], "output_columns": ["value", "atr_percent"],
    },
    "tech.bollinger": {
        "name_en": "Bollinger Bands", "unit": "usd", "value_range": "价格带",
        "formula": "Mid=SMA(C,N); Upper/Lower=Mid±kσ; %B=(C−Lower)/(Upper−Lower); Bandwidth=(Upper−Lower)/Mid",
        "percentile_method": "ROLLING_WINDOW",
        "description_simple": "以 20 天均价为中心画出正常波动范围，带宽收窄说明市场正在憋一波大行情。",
        "description_pro": "输出 upper/middle/lower/bandwidth/percent_b；σ 为样本标准差，默认 (20,2)。",
        "historical_meaning": "Bandwidth 极度收窄后常出现方向性突破，但方向不可预判。",
        "notes": "「触轨即反转」是常见误用，强趋势中价格可沿轨运行（walking the band）。",
        "consumed_by": ["Risk Engine", "行情页"],
        "data_dependencies": ["candles(close)"],
        "output_columns": ["upper", "middle", "lower", "bandwidth", "percent_b", "value"],
    },
    "tech.vwap": {
        "name_en": "Volume Weighted Average Price", "unit": "usd", "value_range": ">0",
        "formula": "VWAP = Σ(TypicalPrice×Volume)/Σ(Volume), TypicalPrice=(H+L+C)/3",
        "percentile_method": "ROLLING_WINDOW",
        "description_simple": "今天（或本周/本月）按成交量加权的平均成交价，代表参与者平均持仓成本。",
        "description_pro": "按锚定周期重置累积（session/week/month/none）；输出 value 与 vwap_deviation 乖离率。",
        "historical_meaning": "机构执行算法普遍以 VWAP 为基准，价格回踩周/月 VWAP 常出现承接。",
        "notes": "加密 7×24 交易，日内以 UTC 00:00 为界并在界面注明。",
        "consumed_by": ["行情页", "Backtest", "Risk Engine"],
        "data_dependencies": ["candles(high, low, close, volume)"], "output_columns": ["value", "vwap_deviation"],
    },
    "tech.hv": {
        "name_en": "Historical Volatility", "unit": "percent", "value_range": ">=0",
        "formula": "r_t=ln(C_t/C_{t-1}); HV(N)=std(r,N)×√365",
        "percentile_method": "FULL_HISTORY",
        "description_simple": "过去一个多月价格起伏的剧烈程度（年化），越大市场越不稳定。",
        "description_pro": "对数收益率滚动标准差年化（365）；输出 value（小数）与 hv_percent（百分比）。",
        "historical_meaning": "HV30>80% 常对应恐慌或狂热；HV 长期低位常出现在筑底期。",
        "notes": "与期权 IV 对比须同口径年化；极端单日波动会抬升短窗口 HV。",
        "consumed_by": ["Risk Engine", "Valuation Engine", "期权页"],
        "data_dependencies": ["candles(close)"], "output_columns": ["value", "hv_percent"],
    },
    "tech.percentile_rank": {
        "name_en": "Percentile Rank", "unit": "percent", "value_range": "0~100",
        "formula": "PctRank(x_t,N) = count(x_i<x_t, i∈[t−N,t))/N × 100%",
        "percentile_method": "FULL_HISTORY / ROLLING_WINDOW",
        "description_simple": "告诉你现在的数值在历史上排第几，90% 表示历史上 90% 的时间都比现在低。",
        "description_pro": "通用分位工具，支持滚动窗口与全历史扩展两种模式；是全部引擎打分的主要输入形式。",
        "historical_meaning": ">90% 或 <10% 表示极端状态，50% 附近为中性。",
        "notes": "样本数 <365 时应标记「样本不足，分位参考意义有限」。",
        "consumed_by": ["全部引擎", "所有指标详情页"],
        "data_dependencies": ["任意 indicator_values / 标准化数据表"], "output_columns": ["value"],
    },
    # ---- 链上指标 ----
    "onchain.mvrv": {
        "name_en": "Market Value to Realized Value", "unit": "ratio", "value_range": ">0",
        "formula": "MVRV = Market Cap / Realized Cap；MVRV-Z = (MCap−RCap)/StdDev(MCap)",
        "percentile_method": "FULL_HISTORY",
        "description_simple": "衡量当前价格相对链上平均成本的倍数，>1 市场整体盈利，<1 整体亏损。",
        "description_pro": "Market Cap=Close×Supply；输出 value 与 mvrv_z；缺列时自动由 close×supply 推算。",
        "historical_meaning": ">3.5 通常对应周期顶部区域，<1 通常对应底部区域。",
        "notes": "不同 Provider 的 Realized Cap 口径可能有差异；早期（2013 前）分位参考意义弱。",
        "consumed_by": ["Valuation Engine", "Cycle Engine", "Risk Engine", "定投模拟"],
        "data_dependencies": ["market_prices(market_cap/close)", "onchain_metrics(realized_cap, supply)"],
        "output_columns": ["value", "mvrv_z"],
    },
    "onchain.sopr": {
        "name_en": "Spent Output Profit Ratio", "unit": "ratio", "value_range": "围绕 1",
        "formula": "SOPR = Σ(花费 UTXO 产出价值)/Σ(花费 UTXO 创造价值)",
        "percentile_method": "ROLLING_WINDOW",
        "description_simple": "今天卖币的人平均是赚钱还是亏钱，>1 获利了结，<1 割肉离场。",
        "description_pro": "输出 value 与 7 日移动平均 sma7；引擎消费其 7DMA 以降噪。",
        "historical_meaning": "牛市回踩 1 获支撑，熊市反弹至 1 受阻；持续 <1 对应投降式抛售。",
        "notes": "对交易所内部转账敏感，各 Provider 过滤口径不同；单日值噪音大。",
        "consumed_by": ["Cycle Engine", "Valuation Engine", "Risk Engine"],
        "data_dependencies": ["onchain_metrics(sopr)"], "output_columns": ["value", "sma7"],
    },
    "onchain.sopr.adjusted": {
        "name_en": "Adjusted SOPR", "unit": "ratio", "value_range": "围绕 1",
        "formula": "同 SOPR，剔除寿命 <1 小时的 UTXO",
        "percentile_method": "ROLLING_WINDOW",
        "description_simple": "和 SOPR 含义相同，但剔除机器人与交易所内部转账噪音，更反映真实盈亏行为。",
        "description_pro": "输出 value 与 sma7；1 附近的支撑/压力信号比原始 SOPR 更可靠。",
        "historical_meaning": "与 SOPR 相同。", "notes": "部分 Provider 不提供 aSOPR。",
        "consumed_by": ["Cycle Engine", "Valuation Engine", "Risk Engine"],
        "data_dependencies": ["onchain_metrics(asopr)"], "output_columns": ["value", "sma7"],
    },
    "onchain.sopr.lth": {
        "name_en": "Long-Term Holder SOPR", "unit": "ratio", "value_range": "围绕 1",
        "formula": "仅统计币龄 >155 天的花费 UTXO 的 SOPR",
        "percentile_method": "ROLLING_WINDOW",
        "description_simple": "只看拿币超过 5 个月的老币民卖币时是赚是亏。",
        "description_pro": "输出 value 与 sma7；LTH-SOPR 从高位回落至 1 企稳常对应牛市深度回调。",
        "historical_meaning": "持续 <1 数月对应熊市投降段。",
        "notes": "155 天口径为行业惯例而非真理。",
        "consumed_by": ["Cycle Engine", "Valuation Engine", "Risk Engine"],
        "data_dependencies": ["onchain_metrics(lth_sopr)"], "output_columns": ["value", "sma7"],
    },
    "onchain.nupl": {
        "name_en": "Net Unrealized Profit / Loss", "unit": "ratio", "value_range": "-1~1",
        "formula": "NUPL = (Market Cap − Realized Cap)/Market Cap = 1 − 1/MVRV",
        "percentile_method": "FULL_HISTORY",
        "description_simple": "全市场账面上有多少比例的利润还没落袋，越高获利了结压力越大，为负说明整体亏损。",
        "description_pro": "与 MVRV 数学同源，引擎中不可重复计权；输出 value。",
        "historical_meaning": ">0.75 仅出现在周期顶部区域；<0 对应大底（全网整体亏损）。",
        "notes": "Market Cap 与 Realized Cap 的 supply 口径须一致。",
        "consumed_by": ["Valuation Engine", "Cycle Engine", "定投模拟"],
        "data_dependencies": ["market_prices(market_cap)", "onchain_metrics(realized_cap)"],
        "output_columns": ["value"],
    },
    "onchain.puell_multiple": {
        "name_en": "Puell Multiple", "unit": "ratio", "value_range": ">0",
        "formula": "Puell = 当日矿工收入(USD) / 365 日 MA 矿工收入(USD)",
        "percentile_method": "FULL_HISTORY",
        "description_simple": "矿工今天收入是过去一年平均的几倍，收入异常高常接近顶部，被压到很低常接近底部。",
        "description_pro": "MA 窗口固定 365 天；输出 value。",
        "historical_meaning": ">4 多次对应周期顶部附近；<0.5 多次对应底部区域。",
        "notes": "⚠️ 减半后 1 年内 Puell 系统性偏低，解读必须结合减半日历。",
        "consumed_by": ["Cycle Engine", "Valuation Engine"],
        "data_dependencies": ["onchain_metrics(miner_revenue_usd)"], "output_columns": ["value"],
    },
    "onchain.rhodl": {
        "name_en": "RHODL Ratio", "unit": "ratio", "value_range": ">0",
        "formula": "RHODL = STH 供应比例 / LTH 供应比例（48 周口径）",
        "percentile_method": "FULL_HISTORY",
        "description_simple": "衡量市场上是新人多还是老手多，新人占比冲高常接近顶部，极低常接近底部。",
        "description_pro": "48 周口径与通用 155 天不同；缺直接列时由 sth/lth 供应推算。",
        "historical_meaning": "冲至历史高分位后均出现深度回调；跌至低分位对应熊市底部。",
        "notes": "作为独立因子，避免与 LTH Supply 重复计权。",
        "consumed_by": ["Cycle Engine", "Risk Engine"],
        "data_dependencies": ["onchain_metrics(rhodl_ratio 或 sth_supply_48w, lth_supply_48w)"],
        "output_columns": ["value"],
    },
    "onchain.reserve_risk": {
        "name_en": "Reserve Risk", "unit": "ratio", "value_range": ">0（对数展示）",
        "formula": "Reserve Risk = Price / HODL Bank",
        "percentile_method": "FULL_HISTORY",
        "description_simple": "价格相对大家囤币信心的高低，老币民拿住不动时该值很低，历史上是较好的长期买点区域。",
        "description_pro": "输出 value 与 log10（对数轴展示）；缺直接列时由 price/hodl_bank 推算。",
        "historical_meaning": "历史低分位（<10%）多次对应大级别底部；高分位对应顶部区域。",
        "notes": "数值跨越多个数量级，展示必须用对数轴。",
        "consumed_by": ["Valuation Engine", "Cycle Engine"],
        "data_dependencies": ["onchain_metrics(reserve_risk 或 price + hodl_bank)"],
        "output_columns": ["value", "log10"],
    },
    # ---- 衍生品指标 ----
    "deriv.funding_rate": {
        "name_en": "Funding Rate", "unit": "percent", "value_range": "可正可负",
        "formula": "年化 Funding = Rate × 每日结算次数 × 365",
        "percentile_method": "ROLLING_WINDOW",
        "description_simple": "合约市场里多头和空头谁更急，费率高说明做多拥挤市场偏热，深度为负有逼空风险。",
        "description_pro": "输出 value/annualized/annualized_percent/rolling_mean/extreme；极端检测阈值可配置。",
        "historical_meaning": "年化 >30% 对应多头拥挤；<−20% 对应空头拥挤，多次触发逼空反弹。",
        "notes": "不同结算周期费率不可直接相加，年化后才能对比。",
        "consumed_by": ["Risk Engine", "Cycle Engine", "Market Regime"],
        "data_dependencies": ["derivatives(funding_rate)"],
        "output_columns": ["value", "annualized", "annualized_percent", "rolling_mean", "extreme"],
    },
    "deriv.open_interest": {
        "name_en": "Open Interest", "unit": "usd", "value_range": ">=0",
        "formula": "OI(USD)=OI(BTC)×标记价；OI_change=OI_t/OI_{t-w}−1；corr=corr(ΔOI, ΔPrice)",
        "percentile_method": "EXPANDING_FROM",
        "description_simple": "合约市场还有多少仓位没平，衡量杠杆资金总规模，OI 快速上升说明杠杆涌入。",
        "description_pro": "输出 value/oi_change/price_corr/oi_mcap_ratio；用于背离检测。",
        "historical_meaning": "OI/MCap 创新高+Funding 高位对应杠杆过热；OI 骤降常伴随大幅波动。",
        "notes": "OI 绝对值受市场规模增长影响，禁止直接用全历史分位判过热，须用 OI/MCap。",
        "consumed_by": ["Risk Engine", "Cycle Engine", "Market Regime"],
        "data_dependencies": ["derivatives(open_interest, open_interest_usd)"],
        "output_columns": ["value", "oi_change", "price_corr", "oi_mcap_ratio"],
    },
    "deriv.cvd": {
        "name_en": "Cumulative Volume Delta", "unit": "btc", "value_range": "可正可负",
        "formula": "Delta(t)=TakerBuy(t)−TakerSell(t); CVD=ΣDelta（自锚定点累计）",
        "percentile_method": "ROLLING_WINDOW",
        "description_simple": "把主动买入减主动卖出的差额累加，看资金是持续净流入还是净流出。",
        "description_pro": "输出 value（CVD）与 volume_delta；支持 day/week/month/none 锚定重置。",
        "historical_meaning": "价格新高+CVD 背离历史上多次先于顶部。",
        "notes": "须区分现货 CVD 与永续 CVD，两者背离本身是信号。",
        "consumed_by": ["Risk Engine", "Cycle Engine", "行情页"],
        "data_dependencies": ["derivatives(taker_buy_volume, taker_sell_volume)"],
        "output_columns": ["value", "volume_delta"],
    },
    # ---- 复合指标 ----
    "composite.ath": {
        "name_en": "All Time High", "unit": "usd", "value_range": ">0",
        "formula": "ATH_t = max(Close_0..t)（Point-in-Time 累积最大值）",
        "percentile_method": "FULL_HISTORY",
        "description_simple": "截至当天为止的历史最高价。",
        "description_pro": "采用 cum_max 累积最大值，严格 Point-in-Time，避免回测未来数据泄漏。",
        "historical_meaning": "价格接近 ATH 时常伴随情绪高涨。",
        "notes": "回测时不得使用模拟时刻之后的 ATH。",
        "consumed_by": ["Valuation Engine", "Cycle Engine"],
        "data_dependencies": ["candles(high)", "market_prices(close)"], "output_columns": ["value"],
    },
    "composite.distance_from_ath": {
        "name_en": "Distance From ATH", "unit": "percent", "value_range": "<=0",
        "formula": "DistanceFromATH = Price / ATH − 1",
        "percentile_method": "FULL_HISTORY",
        "description_simple": "当前价格距离历史最高点跌了百分之多少。",
        "description_pro": "输出 value（小数）与 distance_percent（百分比）。",
        "historical_meaning": "−20% 以内属正常波动，−70% 以上仅出现在深度熊市。",
        "notes": "ATH 必须 Point-in-Time 计算。",
        "consumed_by": ["Valuation Engine", "Risk Engine"],
        "data_dependencies": ["candles(high)", "market_prices(close)"],
        "output_columns": ["value", "distance_percent"],
    },
    "composite.ath_breakout": {
        "name_en": "ATH Breakout", "unit": "count", "value_range": "{0,1}",
        "formula": "Breakout_t = 1 if Price_t >= ATH_{t-1} else 0",
        "percentile_method": "N/A",
        "description_simple": "当期价格是否创出了新的历史最高价。",
        "description_pro": "输出 value（1=突破/0=未突破），首个数据点视为突破。",
        "historical_meaning": "突破 ATH 常伴随动量行情，但也可能是顶部派发。",
        "notes": "须结合成交量与衍生品数据判断突破质量。",
        "consumed_by": ["Cycle Engine", "行情页"],
        "data_dependencies": ["candles(high)", "market_prices(close)"], "output_columns": ["value"],
    },
    "composite.drawdown": {
        "name_en": "Drawdown from ATH / Rolling High", "unit": "percent", "value_range": "<=0",
        "formula": "DD_ATH = Close/ATH − 1；DD_RH(N) = Close/max(Close,N) − 1",
        "percentile_method": "FULL_HISTORY",
        "description_simple": "当前价格距离历史最高点跌了百分之多少，跌得越深越接近历史恐慌区域。",
        "description_pro": "输出 value/dd_percent，rolling_window>0 时另输出 dd_rolling。",
        "historical_meaning": "−30%~−50% 多对应中期调整或熊市早段。",
        "notes": "ATH Point-in-Time 计算，是未来数据泄漏高发点。",
        "consumed_by": ["Valuation Engine", "Risk Engine", "Cycle Engine", "定投模拟"],
        "data_dependencies": ["market_prices(close)", "candles(high)"],
        "output_columns": ["value", "dd_percent", "dd_rolling"],
    },
    "composite.max_drawdown": {
        "name_en": "Maximum Drawdown", "unit": "percent", "value_range": "<=0",
        "formula": "MaxDrawdown_t = min(DD_ATH_0..t)（累积最小值）",
        "percentile_method": "FULL_HISTORY",
        "description_simple": "历史上从最高点跌到最低点的最深跌幅。",
        "description_pro": "value 为截至当期的累积最深回撤；metadata 含整段样本 max_drawdown。",
        "historical_meaning": "BTC 历史多次出现 −70% 以上的深度回撤。",
        "notes": "Point-in-Time 累积，回测安全。",
        "consumed_by": ["Risk Engine", "Backtest"],
        "data_dependencies": ["market_prices(close)"], "output_columns": ["value"],
    },
    "composite.drawdown_duration": {
        "name_en": "Drawdown Duration", "unit": "count", "value_range": ">=0",
        "formula": "Duration_t = t − index(last ATH)",
        "percentile_method": "FULL_HISTORY",
        "description_simple": "当前这波回撤已经持续了多久（多少个周期）。",
        "description_pro": "创新高当期归零；metadata 含 current_duration。",
        "historical_meaning": "长持续时间叠加深度回撤常对应熊市底部区域。",
        "notes": "周期单位取决于输入数据频率（日线为天数）。",
        "consumed_by": ["Cycle Engine", "Risk Engine"],
        "data_dependencies": ["market_prices(close)"], "output_columns": ["value"],
    },
}


class IndicatorRegistry:
    """指标注册中心（单例）。"""

    def __init__(self) -> None:
        self._classes: dict[str, type[IndicatorBase]] = {}
        self._instances: dict[str, IndicatorBase] = {}
        self._loaded: bool = False

    # ------------------------------------------------------------------ #
    # 注册
    # ------------------------------------------------------------------ #
    def register(self, indicator_cls: type[IndicatorBase], *, override: bool = False) -> None:
        """注册单个指标类。

        Parameters
        ----------
        indicator_cls:
            :class:`IndicatorBase` 子类（类对象，非实例）。
        override:
            同名指标是否允许覆盖，默认 False（重复注册抛错）。
        """
        if not (isinstance(indicator_cls, type) and issubclass(indicator_cls, IndicatorBase)):
            raise TypeError(f"{indicator_cls!r} 必须是 IndicatorBase 的子类")
        # 实例化以读取 name（属性为实例 property）
        probe = indicator_cls()
        name = probe.name
        if name in self._classes and not override:
            raise ValueError(f"指标 {name!r} 已注册，重复注册需显式 override=True")
        self._classes[name] = indicator_cls
        self._instances[name] = probe

    def register_all(self, *, override: bool = True) -> int:
        """自动发现并注册全部子包指标类。

        Returns
        -------
        int
            成功注册的指标数量。
        """
        from . import composite, derivatives, onchain, technical

        count = 0
        for module in (technical, onchain, derivatives, composite):
            for cls in getattr(module, "INDICATOR_CLASSES", []):
                self.register(cls, override=override)
                count += 1
        self._loaded = True
        return count

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.register_all()

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def get_indicator(self, name: str) -> IndicatorBase:
        """按唯一标识名获取指标实例。

        Raises
        ------
        KeyError
            指标未注册时抛出。
        """
        self._ensure_loaded()
        if name not in self._instances:
            raise KeyError(f"未注册的指标: {name!r}；可用: {sorted(self._instances)}")
        return self._instances[name]

    # 兼容别名
    get = get_indicator

    def names(self, category: str | None = None) -> list[str]:
        """返回全部（或指定分类）指标名列表。"""
        self._ensure_loaded()
        if category is None:
            return sorted(self._instances)
        return sorted(n for n, ind in self._instances.items() if ind.category == category)

    def list_indicators(self, category: str | None = None) -> list[dict[str, Any]]:
        """返回指标摘要列表。

        Parameters
        ----------
        category:
            可选分类过滤：technical / onchain / derivatives / composite。

        Returns
        -------
        list[dict]
            每项含 name / display_name / category / default_params / required_data。
        """
        self._ensure_loaded()
        result: list[dict[str, Any]] = []
        for name in self.names(category):
            ind = self._instances[name]
            result.append(
                {
                    "name": name,
                    "display_name": ind.display_name,
                    "category": ind.category,
                    "default_params": dict(ind.default_params),
                    "required_data": list(ind.required_data),
                }
            )
        return result

    def categories(self) -> list[str]:
        """返回已注册指标覆盖的全部分类。"""
        self._ensure_loaded()
        return sorted({ind.category for ind in self._instances.values()})

    def get_indicator_definition(self, name: str) -> dict[str, Any]:
        """返回指标完整定义（普通解释 + 专业详情）。

        将实例动态属性（display_name / category / default_params / required_data）
        与静态元数据字典 :data:`INDICATOR_DEFINITIONS` 合并输出，
        可直接用于写入 ``indicator_definitions`` 表或 API 返回。

        Raises
        ------
        KeyError
            指标未注册时抛出。
        """
        self._ensure_loaded()
        ind = self.get_indicator(name)
        static = dict(INDICATOR_DEFINITIONS.get(name, {}))
        definition: dict[str, Any] = {
            "code": name,
            "name": static.pop("name_en", ind.display_name),
            "name_cn": ind.display_name,
            "category": ind.category,
            "default_params": dict(ind.default_params),
            "data_dependencies": static.pop("data_dependencies", list(ind.required_data)),
            "required_data": list(ind.required_data),
            "output_columns": static.pop("output_columns", []),
            # 双模式解释
            "interpretation": {
                "simple": static.pop("description_simple", ""),
                "pro": static.pop("description_pro", ""),
                "historical_meaning": static.pop("historical_meaning", ""),
                "notes": static.pop("notes", ""),
            },
            "consumed_by": static.pop("consumed_by", []),
        }
        # 其余字段（formula / unit / value_range / percentile_method）平铺
        definition.update(static)
        return definition


#: 全局注册中心单例
registry = IndicatorRegistry()


# --------------------------------------------------------------------------- #
# 模块级便捷函数
# --------------------------------------------------------------------------- #
def get_indicator(name: str) -> IndicatorBase:
    """从全局注册中心获取指标实例。"""
    return registry.get_indicator(name)


def list_indicators(category: str | None = None) -> list[dict[str, Any]]:
    """列出全部（或指定分类）指标摘要。"""
    return registry.list_indicators(category)


def get_indicator_definition(name: str) -> dict[str, Any]:
    """获取指标完整定义（普通解释 + 专业详情）。"""
    return registry.get_indicator_definition(name)


# 分类常量再导出，便于外部统一引用
_ = IndicatorCategory
