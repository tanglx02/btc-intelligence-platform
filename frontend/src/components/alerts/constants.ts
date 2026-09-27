/**
 * 智能预警模块 UI 常量：监测数据 / 条件 / 时长 / 冷却 / 严重程度选项。
 *
 * metric 代码与 backend/app/alerts/context_builder.py（resolve_value）及
 * IndicatorDefinition.code 对齐；引擎类指标以 "engine:" 前缀表示
 * （对应 state_change 条件的 engine 字段）。
 */

import type { ConditionId, Severity4 } from "./types";

/* -------------------------------------------------------------------------- */
/* 监测数据                                                                    */
/* -------------------------------------------------------------------------- */

export interface MetricOption {
  value: string;
  label: string;
  /** 数值单位（预览与阈值后缀展示） */
  unit?: string;
  /** ℹ️ hover 解释：这个条件代表什么？ */
  hint: string;
  /** 展示小数位数（默认自适应） */
  decimals?: number;
  /** 引擎类指标（state_change），值为引擎名 */
  engine?: string;
  /** change_pct 涨跌幅类（作用对象为价格） */
  isChange?: boolean;
}

export const METRIC_OPTIONS: MetricOption[] = [
  {
    value: "price",
    label: "BTC 价格",
    unit: "USDT",
    decimals: 2,
    hint: "BTC/USDT 最新成交价，多数据源交叉验证后取中位数，是最直观的提醒基准。",
  },
  {
    value: "change",
    label: "BTC 涨跌幅",
    unit: "%",
    decimals: 2,
    isChange: true,
    hint: "BTC 在指定时间窗口（1小时 / 24小时 / 7天）内的涨跌百分比，用于捕捉急涨急跌。",
  },
  {
    value: "mvrv",
    label: "MVRV",
    unit: "倍",
    decimals: 2,
    hint: "市值 / 实现市值。大于 3.5 常见于历史顶部区域，低于 1 说明整体持仓处于浮亏（底部区域参考）。",
  },
  {
    value: "sopr",
    label: "SOPR",
    unit: "",
    decimals: 3,
    hint: "花费产出利润率：链上转账的实现利润比。低于 1 表示整体在亏损卖出，历史上常见于恐慌抛售阶段。",
  },
  {
    value: "nupl",
    label: "NUPL",
    unit: "",
    decimals: 2,
    hint: "未实现净利润占比，衡量全市场浮盈水平；接近 0.75+ 为贪婪顶部，0 附近为 capitulation 底部。",
  },
  {
    value: "fear_greed",
    label: "恐惧贪婪指数",
    unit: "",
    decimals: 0,
    hint: "0~100 的市场情绪合成指数。低于 20 为极端恐慌，高于 80 为极度贪婪，常作为逆向情绪参考。",
  },
  {
    value: "funding_rate",
    label: "资金费率",
    unit: "",
    decimals: 6,
    hint: "永续合约多空双方支付的资金费率。持续为正且偏高说明多头杠杆拥挤，容易出现多头清算回调。",
  },
  {
    value: "open_interest_change_24h",
    label: "持仓量 24h 变化",
    unit: "%",
    decimals: 2,
    hint: "全市场永续合约未平仓量（OI）的 24 小时变化百分比，快速上升代表杠杆资金涌入。",
  },
  {
    value: "etf_flow_1d",
    label: "ETF 单日净流入",
    unit: "亿美元",
    decimals: 2,
    hint: "美国现货 BTC ETF 当日净流入（负数为净流出）。大额流入/流出常伴随行情波动。",
  },
  {
    value: "drawdown_pct",
    label: "距历史高点回撤",
    unit: "%",
    decimals: 2,
    hint: "当前价格距离历史最高点的跌幅（负数）。-20% 进入技术性熊市参考线，-50% 以上为深熊。",
  },
  {
    value: "rsi",
    label: "RSI (14)",
    unit: "",
    decimals: 1,
    hint: "14 日相对强弱指标。低于 30 为超卖、高于 70 为超买，适用于震荡市反转参考。",
  },
  {
    value: "risk_score",
    label: "综合风险评分",
    unit: "",
    decimals: 1,
    hint: "风险引擎输出的 0~100 综合评分，融合估值、杠杆、链上与情绪维度，越高代表当前市场越危险。",
  },
  {
    value: "portfolio_pnl_pct",
    label: "我的持仓盈亏",
    unit: "%",
    decimals: 2,
    hint: "你在「我的资产」中的组合累计收益率，可用于设置止盈 / 止损级别的自我提醒。",
  },
  {
    value: "engine:cycle",
    label: "周期状态",
    hint: "周期引擎判断的市场阶段（如积累期、上升期、派发期、顶部风险）。选择后条件为「状态变化为」。",
    engine: "cycle",
  },
  {
    value: "engine:valuation",
    label: "估值状态",
    hint: "估值引擎的五档结论（深度低估 → 极度高估）。选择后条件为「状态变化为」。",
    engine: "valuation",
  },
  {
    value: "engine:risk",
    label: "风险状态",
    hint: "风险引擎的档位结论（低 → 极端）。选择后条件为「状态变化为」。",
    engine: "risk",
  },
  {
    value: "engine:regime",
    label: "综合市场状态",
    hint: "Regime 引擎综合九大维度的一句话市场状态。选择后条件为「状态变化为」。",
    engine: "regime",
  },
];

export const METRIC_MAP: Record<string, MetricOption> = Object.fromEntries(
  METRIC_OPTIONS.map((m) => [m.value, m]),
);

export function metricLabel(metric?: string | null): string {
  if (!metric) return "未知指标";
  return METRIC_MAP[metric]?.label ?? metric;
}

/* -------------------------------------------------------------------------- */
/* 条件类型                                                                    */
/* -------------------------------------------------------------------------- */

export type ConditionKind = "threshold" | "change" | "range" | "cross" | "percentile" | "state";

export interface ConditionOption {
  value: ConditionId;
  label: string;
  kind: ConditionKind;
  /** 普通模式下该条件的数值输入提示 */
  placeholder?: string;
}

export const CONDITION_OPTIONS: ConditionOption[] = [
  { value: "gt", label: "高于", kind: "threshold", placeholder: "阈值" },
  { value: "lt", label: "低于", kind: "threshold", placeholder: "阈值" },
  { value: "change_up", label: "涨超过", kind: "change", placeholder: "涨幅 %" },
  { value: "change_down", label: "跌超过", kind: "change", placeholder: "跌幅 %" },
  { value: "range_enter", label: "进入区间", kind: "range", placeholder: "下限" },
  { value: "range_exit", label: "离开区间", kind: "range", placeholder: "下限" },
  { value: "cross_above", label: "穿越上行（上穿）", kind: "cross", placeholder: "参考线" },
  { value: "cross_below", label: "穿越下行（下穿）", kind: "cross", placeholder: "参考线" },
  { value: "percentile_above", label: "历史分位高于", kind: "percentile", placeholder: "分位 %" },
  { value: "percentile_below", label: "历史分位低于", kind: "percentile", placeholder: "分位 %" },
  { value: "state_change", label: "状态变化为", kind: "state" },
];

export const CONDITION_MAP: Record<string, ConditionOption> = Object.fromEntries(
  CONDITION_OPTIONS.map((c) => [c.value, c]),
);

/** 涨跌幅窗口 */
export const WINDOW_OPTIONS: { value: number; label: string }[] = [
  { value: 1, label: "1 小时" },
  { value: 24, label: "24 小时" },
  { value: 168, label: "7 天" },
];

/** 状态变化目标（引擎输出的常见状态，允许自由填写） */
export const ENGINE_STATE_SUGGESTIONS: Record<string, string[]> = {
  cycle: ["DEEP_BEAR", "BEAR", "BOTTOM_BUILDING", "RECOVERY", "UPTREND", "ACCELERATION", "DISTRIBUTION", "TOP_RISK", "DECLINE"],
  valuation: ["DEEP_UNDERVALUED", "UNDERVALUED", "FAIR", "OVERVALUED", "EXTREME_OVERVALUED"],
  risk: ["VERY_LOW", "LOW", "MODERATE", "HIGH", "VERY_HIGH", "EXTREME"],
};

/* -------------------------------------------------------------------------- */
/* 时长 / 冷却 / 严重程度                                                        */
/* -------------------------------------------------------------------------- */

export const DURATION_PRESETS: { key: string; label: string; seconds: number | null }[] = [
  { key: "none", label: "不限", seconds: null },
  { key: "5m", label: "5 分钟", seconds: 300 },
  { key: "15m", label: "15 分钟", seconds: 900 },
  { key: "1h", label: "1 小时", seconds: 3600 },
  { key: "4h", label: "4 小时", seconds: 14400 },
  { key: "1d", label: "1 天", seconds: 86400 },
];

export const COOLDOWN_PRESETS: { key: string; label: string; seconds: number }[] = [
  { key: "5m", label: "5 分钟", seconds: 300 },
  { key: "15m", label: "15 分钟", seconds: 900 },
  { key: "1h", label: "1 小时", seconds: 3600 },
  { key: "4h", label: "4 小时", seconds: 14400 },
  { key: "12h", label: "12 小时", seconds: 43200 },
  { key: "24h", label: "24 小时", seconds: 86400 },
  { key: "3d", label: "3 天", seconds: 259200 },
  { key: "7d", label: "7 天", seconds: 604800 },
  { key: "custom", label: "自定义", seconds: -1 },
];

export const COOLDOWN_MAP: Record<string, number> = Object.fromEntries(
  COOLDOWN_PRESETS.map((p) => [p.key, p.seconds]),
);

export const SEVERITY_OPTIONS: {
  value: Severity4;
  label: string;
  desc: string;
}[] = [
  { value: "INFO", label: "提示 INFO", desc: "供参考的信息，如阶段变化" },
  { value: "WARNING", label: "警告 WARNING", desc: "值得关注的信号，建议浏览详情" },
  { value: "HIGH", label: "重要 HIGH", desc: "关键阈值突破，建议尽快查看" },
  { value: "CRITICAL", label: "严重 CRITICAL", desc: "极端行情 / 高风险事件" },
];

/** 周报星期（weekly_day 0-6） */
export const WEEKDAY_OPTIONS: { value: number; label: string }[] = [
  { value: 0, label: "周日" },
  { value: 1, label: "周一" },
  { value: 2, label: "周二" },
  { value: 3, label: "周三" },
  { value: 4, label: "周四" },
  { value: 5, label: "周五" },
  { value: 6, label: "周六" },
];

/** 规则分类（模板 category -> AlertRuleCreate.category） */
export const TEMPLATE_CATEGORY_MAP: Record<string, string> = {
  PRICE: "MARKET",
  RISK: "INDICATOR",
  SENTIMENT: "INDICATOR",
  VALUATION: "INDICATOR",
  DERIVATIVES: "INDICATOR",
  FLOW: "PROVIDER",
  ENGINE: "ENGINE",
  TECHNICAL: "INDICATOR",
  PORTFOLIO: "PORTFOLIO",
};
