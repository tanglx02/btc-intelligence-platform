/**
 * 市场页面组共享工具：数字格式化、状态语义映射、防御性数据解析。
 *
 * 后端各端点返回结构多为宽松 JSON（provider 透传），这里提供统一的
 * 取数（pickNumber/pickString）与序列抽取（extractSeriesRecords）辅助，
 * 保证 UI 在字段命名差异下依然稳健降级。
 */

/* -------------------------------------------------------------------------- */
/* 基础类型                                                                    */
/* -------------------------------------------------------------------------- */

/** 语义色调（与 StateCard 的 StateColor 对齐） */
export type StateTone = "up" | "down" | "warn" | "accent" | "muted";

/** 图表用主题色（与 globals.css 设计 token 一致） */
export const CHART = {
  text: "#8b949e",
  axis: "#30363d",
  grid: "#21262d",
  up: "#3fb950",
  down: "#f85149",
  accent: "#58a6ff",
  warn: "#d29922",
  orange: "#db6d28",
  muted: "#8b949e",
  fg: "#e6edf3",
} as const;

/* -------------------------------------------------------------------------- */
/* 防御性取数                                                                  */
/* -------------------------------------------------------------------------- */

/** 从宽松对象中按候选键取第一个有限数字（字符串数字亦接受） */
export function pickNumber(obj: unknown, keys: string[]): number | null {
  if (typeof obj === "number") return Number.isFinite(obj) ? obj : null;
  if (!obj || typeof obj !== "object") return null;
  const o = obj as Record<string, unknown>;
  for (const k of keys) {
    const v = o[k];
    if (typeof v === "number" && Number.isFinite(v)) return v;
    if (typeof v === "string" && v.trim() !== "" && Number.isFinite(Number(v))) {
      return Number(v);
    }
  }
  return null;
}

/** 从宽松对象中按候选键取第一个非空字符串 */
export function pickString(obj: unknown, keys: string[]): string | null {
  if (typeof obj === "string") return obj;
  if (!obj || typeof obj !== "object") return null;
  const o = obj as Record<string, unknown>;
  for (const k of keys) {
    const v = o[k];
    if (typeof v === "string" && v.trim() !== "") return v;
    if (typeof v === "number") return String(v);
  }
  return null;
}

/** 时间字段候选键（后端各表命名不一） */
const TIME_KEYS = ["date", "time", "observation_time", "timestamp", "day", "trade_date"] as const;
/** 数值字段候选键 */
const VALUE_KEYS = [
  "value", "v", "close", "price", "amount", "net_flow", "netflow", "funding_rate",
] as const;

export interface SeriesPoint {
  /** 原始时间字符串（可能是日期 / ISO 时间戳），可能为空 */
  time: string;
  value: number;
}

/**
 * 从任意宽松结构中抽取 { time, value } 序列。
 * 支持：数组直出；对象包裹于 history/series/rows/points/data/daily_flows 等键下。
 */
export function extractSeriesRecords(raw: unknown): SeriesPoint[] {
  let arr: unknown[] | null = null;
  if (Array.isArray(raw)) {
    arr = raw;
  } else if (raw && typeof raw === "object") {
    const obj = raw as Record<string, unknown>;
    for (const key of ["history", "series", "rows", "points", "data", "daily_flows", "items"]) {
      const v = obj[key];
      if (Array.isArray(v)) {
        arr = v;
        break;
      }
    }
  }
  if (!arr) return [];

  const out: SeriesPoint[] = [];
  for (const item of arr) {
    if (typeof item === "number" && Number.isFinite(item)) {
      out.push({ time: "", value: item });
      continue;
    }
    if (!item || typeof item !== "object") continue;
    const o = item as Record<string, unknown>;
    const time = pickString(o, [...TIME_KEYS]) ?? "";
    const value = pickNumber(o, [...VALUE_KEYS]);
    if (value !== null) out.push({ time, value });
  }
  return out;
}

/* -------------------------------------------------------------------------- */
/* 数字格式化                                                                  */
/* -------------------------------------------------------------------------- */

/** 大数缩写：1.23T / 1.23B / 45.6M / 789.1K；负数支持 */
export function formatNumber(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const abs = Math.abs(v);
  const sign = v < 0 ? "-" : "";
  if (abs >= 1e12) return `${sign}${(abs / 1e12).toFixed(digits)}T`;
  if (abs >= 1e9) return `${sign}${(abs / 1e9).toFixed(digits)}B`;
  if (abs >= 1e6) return `${sign}${(abs / 1e6).toFixed(digits)}M`;
  if (abs >= 1e4) return `${sign}${(abs / 1e3).toFixed(digits)}K`;
  if (abs >= 100) return `${sign}${abs.toFixed(digits)}`;
  if (abs >= 1) return `${sign}${abs.toFixed(Math.min(digits, 2))}`;
  return `${sign}${abs.toPrecision(3)}`;
}

/** 美元金额（自动缩写）：$1.23B / $45.6M */
export function formatUsd(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return `$${formatNumber(v, digits)}`;
}

/** 价格：千分位 + 2 位小数（>1000 时 0 位小数紧凑） */
export function formatPrice(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  if (Math.abs(v) >= 1000) return v.toLocaleString("en-US", { maximumFractionDigits: 0 });
  return v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** 百分比：+12.3%（v 已是百分数数值，如 2.31 表示 2.31%） */
export function formatPct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(digits)}%`;
}

/** 比率型百分比：0.0001 -> "+0.01%"（资金费率等小数比率） */
export function formatRatioPct(v: number | null | undefined, digits = 3): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const pct = v * 100;
  const sign = pct > 0 ? "+" : "";
  return `${sign}${pct.toFixed(digits)}%`;
}

/** 日期时间（zh-CN，仅客户端调用以避免 hydration 差异） */
export function fmtDateTime(iso?: string | null): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleString("zh-CN", {
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** ISO 日期（本地时区 yyyy-mm-dd） */
export function toISODate(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

/** N 天前的 ISO 日期字符串（供 start/end 查询参数） */
export function daysAgoISO(days: number): string {
  return toISODate(new Date(Date.now() - days * 86_400_000));
}

/** 图表横轴短标签：截取日期部分 */
export function shortDate(t?: string | null): string {
  if (!t) return "";
  return t.length > 10 ? t.slice(0, 10) : t;
}

/** 相对天数：3 天后 / 今天 / 2 天前（仅客户端调用） */
export function relativeDays(iso?: string | null): string | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  const days = Math.round((t - Date.now()) / 86_400_000);
  if (days === 0) return "今天";
  return days > 0 ? `${days} 天后` : `${-days} 天前`;
}

/* -------------------------------------------------------------------------- */
/* 状态语义                                                                    */
/* -------------------------------------------------------------------------- */

const DOWN_RE =
  /DOWNTREND|DOWNTURN|BEAR|DISTRIBUTION|TOP_RISK|CRASH|CAPITUL|EXTREME|OVERVALUED|OUTFLOW|PANIC|DECLIN|WEAK/;
const UP_RE =
  /UPTREND|BULL|RECOVERY|ACCUM|NET_INFLOW|UNDERVALUE|INFLOW|EXPANSION|STRONG|RISING|OPTIMIS/;
const WARN_RE = /MEDIUM|ELEVATED|CAUTION|TRANSITION|RISK|OVERHEAT|STRETCH/;

/** 引擎状态字符串 -> 语义色调（红涨绿跌语义之外的中性归为 muted） */
export function semanticTone(state?: string | null): StateTone {
  const s = (state ?? "").toUpperCase();
  if (!s) return "muted";
  if (DOWN_RE.test(s)) return "down";
  if (UP_RE.test(s)) return "up";
  if (WARN_RE.test(s)) return "warn";
  if (/SIDEWAYS|NEUTRAL|FAIR|MIXED|^LOW$/.test(s)) return "muted";
  if (/HIGH/.test(s)) return "warn";
  return "muted";
}

const STATE_LABELS: Record<string, string> = {
  // 周期
  DEEP_BEAR: "深度熊市",
  BEAR: "熊市",
  BEAR_MARKET: "熊市",
  BOTTOMING: "底部构筑",
  ACCUMULATION: "底部积累",
  RECOVERY: "恢复期",
  UPTREND: "趋势上涨",
  ACCELERATION: "加速上涨",
  EUPHORIA: "狂热期",
  DISTRIBUTION: "高位分配",
  TOP_RISK: "顶部风险",
  DOWNTURN: "下跌中",
  // 估值
  DEEP_UNDERVALUE: "深度低估",
  UNDERVALUE: "低估",
  FAIR: "合理",
  HIGH: "偏高",
  EXTREME_HIGH: "极端高估",
  OVERVALUED: "偏高",
  // 风险档位
  LOW: "低",
  MEDIUM: "中等",
  EXTREME: "极高",
  // 趋势
  UP: "上升",
  DOWN: "下降",
  RISING: "上升",
  FALLING: "下降",
  SIDEWAYS: "横盘",
  // 资金
  NET_INFLOW: "净流入",
  NET_OUTFLOW: "净流出",
  INFLOW: "流入",
  OUTFLOW: "流出",
  // 情绪
  EXTREME_FEAR: "极度恐惧",
  FEAR: "恐惧",
  NEUTRAL: "中性",
  GREED: "贪婪",
  EXTREME_GREED: "极度贪婪",
  // 综合
  BULL: "牛市",
  TRANSITION: "转换期",
  MIXED: "信号混合",
};

/** 状态码 -> 中文标签（未收录时退化为去下划线原文） */
export function stateLabel(state?: string | null): string {
  if (!state) return "—";
  const s = state.trim().toUpperCase();
  return STATE_LABELS[s] ?? s.replace(/_/g, " ");
}

/** Regime / 引擎维度键 -> 中文名 */
const DIMENSION_LABELS: Record<string, string> = {
  trend: "趋势",
  valuation: "估值",
  capital_flow: "资金",
  liquidity: "流动性",
  onchain: "链上",
  derivative: "杠杆",
  derivatives: "杠杆",
  leverage: "杠杆",
  macro: "宏观",
  sentiment: "情绪",
  risk: "风险",
  cycle: "周期",
  overall: "综合",
};

export function labelDimension(key: string): string {
  return DIMENSION_LABELS[key.toLowerCase()] ?? key.replace(/_/g, " ");
}

/** 引擎得分归一化到 0-100（<=1 视作 0-1 比例） */
export function scoreToPct(v: unknown): number {
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return 0;
  const pct = n <= 1 ? n * 100 : n;
  return Math.max(0, Math.min(100, Math.round(pct)));
}

/* -------------------------------------------------------------------------- */
/* 周期 9 阶段                                                                 */
/* -------------------------------------------------------------------------- */

export interface CycleStage {
  keys: string[];
  label: string;
  desc: string;
}

export const CYCLE_STAGES: CycleStage[] = [
  {
    keys: ["DEEP_BEAR"],
    label: "深度熊市",
    desc: "价格深度低于长期持有者成本，市场情绪极度悲观，历史上往往对应周期底部区域。",
  },
  {
    keys: ["BEAR"],
    label: "熊市",
    desc: "价格持续走低、反弹乏力，长期持有者开始逐步积累筹码。",
  },
  {
    keys: ["BOTTOMING", "ACCUMULATION", "BOTTOM"],
    label: "底部构筑",
    desc: "波动收敛、换手充分，长期资金缓慢吸筹，趋势方向尚未确认。",
  },
  {
    keys: ["RECOVERY"],
    label: "恢复期",
    desc: "价格重新站上关键成本线，链上活跃度回升，趋势开始转正。",
  },
  {
    keys: ["UPTREND"],
    label: "趋势上涨",
    desc: "价格与链上指标同步走强，历史上该阶段通常贡献周期内大部分涨幅。",
  },
  {
    keys: ["ACCELERATION", "EUPHORIA", "BULL"],
    label: "加速上涨",
    desc: "涨幅扩大、新资金加速涌入，需要开始关注过热信号。",
  },
  {
    keys: ["DISTRIBUTION"],
    label: "高位分配",
    desc: "长期持有者逐步向新入场者转移筹码，上涨动能减弱。",
  },
  {
    keys: ["TOP_RISK", "TOP"],
    label: "顶部风险",
    desc: "估值与情绪指标处于历史极端高位，回撤风险显著上升。",
  },
  {
    keys: ["DOWNTURN", "CORRECTION"],
    label: "下跌中",
    desc: "趋势转弱、杠杆清算增多，市场进入回调阶段。",
  },
];

/** 当前引擎状态匹配到 9 阶段中的索引（未匹配返回 -1） */
export function matchCycleStageIndex(state?: string | null): number {
  if (!state) return -1;
  const s = state.toUpperCase();
  return CYCLE_STAGES.findIndex((st) => st.keys.some((k) => s.includes(k)));
}

/* -------------------------------------------------------------------------- */
/* 估值五档 / 风险四档                                                         */
/* -------------------------------------------------------------------------- */

export interface GaugeLevel {
  label: string;
  color: string;
}

export const VALUATION_LEVELS: GaugeLevel[] = [
  { label: "深度低估", color: "#3fb950" },
  { label: "低估", color: "#56d364" },
  { label: "合理", color: "#8b949e" },
  { label: "偏高", color: "#d29922" },
  { label: "极端高估", color: "#f85149" },
];

export function valuationLevelIndex(state?: string | null): number {
  const s = (state ?? "").toUpperCase();
  if (!s) return -1;
  if (s.includes("EXTREME")) return 4;
  if (s.includes("DEEP") && s.includes("UNDER")) return 0;
  if (s.includes("UNDER")) return 1;
  if (s.includes("FAIR") || s.includes("NEUTRAL")) return 2;
  if (s.includes("HIGH") || s.includes("OVER")) return 3;
  return -1;
}

export const RISK_LEVELS: GaugeLevel[] = [
  { label: "低", color: "#3fb950" },
  { label: "中", color: "#d29922" },
  { label: "高", color: "#db6d28" },
  { label: "极高", color: "#f85149" },
];

export function riskLevelIndex(state?: string | null): number {
  const s = (state ?? "").toUpperCase();
  if (!s) return -1;
  if (s.includes("EXTREME")) return 3;
  if (s.includes("HIGH")) return 2;
  if (s.includes("MEDIUM") || s.includes("MODERATE") || s.includes("ELEVATED")) return 1;
  if (s.includes("LOW")) return 0;
  return -1;
}

/* -------------------------------------------------------------------------- */
/* 恐惧贪婪                                                                    */
/* -------------------------------------------------------------------------- */

export interface FgBand {
  max: number;
  label: string;
  color: string;
  desc: string;
}

export const FG_BANDS: FgBand[] = [
  {
    max: 24,
    label: "极度恐惧",
    color: "#f85149",
    desc: "市场极度恐慌，历史上往往对应阶段性底部区域，但恐慌本身也可能继续加深。",
  },
  {
    max: 44,
    label: "恐惧",
    color: "#db6d28",
    desc: "投资者情绪偏悲观，抛压与观望情绪主导市场。",
  },
  {
    max: 55,
    label: "中性",
    color: "#d29922",
    desc: "多空情绪相对平衡，市场缺乏一致预期。",
  },
  {
    max: 75,
    label: "贪婪",
    color: "#56d364",
    desc: "情绪偏乐观、追涨意愿增强，需留意过热信号。",
  },
  {
    max: 100,
    label: "极度贪婪",
    color: "#3fb950",
    desc: "情绪极度亢奋，历史上极端贪婪阶段常伴随高波动与回撤风险。",
  },
];

export function fgBand(value: number): FgBand {
  for (const band of FG_BANDS) {
    if (value <= band.max) return band;
  }
  return FG_BANDS[FG_BANDS.length - 1];
}

/** 分类标签（英文/枚举）-> 中文；无标签时按数值推断 */
export function fgClassLabel(raw?: string | null, value?: number | null): string {
  const s = (raw ?? "").toUpperCase().replace(/\s+/g, "_");
  const map: Record<string, string> = {
    EXTREME_FEAR: "极度恐惧",
    FEAR: "恐惧",
    NEUTRAL: "中性",
    GREED: "贪婪",
    EXTREME_GREED: "极度贪婪",
  };
  if (map[s]) return map[s];
  if (value !== null && value !== undefined && Number.isFinite(value)) {
    return fgBand(value).label;
  }
  return raw ? stateLabel(raw) : "—";
}

/* -------------------------------------------------------------------------- */
/* 杂项                                                                        */
/* -------------------------------------------------------------------------- */

export const PLAN_STATUS_LABELS: Record<string, string> = {
  ACTIVE: "进行中",
  PAUSED: "已暂停",
  COMPLETED: "已完成",
  ARCHIVED: "已归档",
  DRAFT: "草稿",
};

export function planStatusLabel(status?: string | null): string {
  if (!status) return "—";
  return PLAN_STATUS_LABELS[status.toUpperCase()] ?? status;
}

/** ECharts 通用 tooltip 深色底（展开到各页 option 中使用） */
export function baseTooltip(): Record<string, unknown> {
  return {
    backgroundColor: "rgba(13,17,23,0.94)",
    borderColor: "#30363d",
    borderWidth: 1,
    textStyle: { color: "#e6edf3", fontSize: 11 },
    extraCssText: "box-shadow: 0 4px 16px rgba(0,0,0,0.4); border-radius: 8px;",
  };
}
