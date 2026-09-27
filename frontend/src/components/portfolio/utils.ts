/**
 * 个人页面组共享工具：格式化、宽松取值、状态映射。
 *
 * 设计原则（docs/architecture/16-page-architecture.md）：
 * - 数字全部等宽对齐（font-mono 由调用方配合 tabular-nums 使用）；
 * - 后端字段宽契约（[key: string]: unknown），用 pickNum/pickStr 容错取值；
 * - 绝不伪造数据：null/undefined 一律渲染占位符「—」。
 */

/* -------------------------------------------------------------------------- */
/* className 合并                                                              */
/* -------------------------------------------------------------------------- */

export function cn(
  ...parts: Array<string | false | null | undefined>
): string {
  return parts.filter(Boolean).join(" ");
}

/* -------------------------------------------------------------------------- */
/* 宽松取值（响应体为宽契约 Record）                                            */
/* -------------------------------------------------------------------------- */

/** 依次尝试多个候选 key，返回第一个有限数值，否则 fallback（默认 null） */
export function pickNum(
  record: Record<string, unknown> | null | undefined,
  keys: string[],
  fallback: number | null = null,
): number | null {
  if (!record) return fallback;
  for (const k of keys) {
    const raw = record[k];
    const n = typeof raw === "string" ? Number(raw) : raw;
    if (typeof n === "number" && Number.isFinite(n)) return n;
  }
  return fallback;
}

/** 依次尝试多个候选 key，返回第一个非空字符串 */
export function pickStr(
  record: Record<string, unknown> | null | undefined,
  keys: string[],
  fallback: string | null = null,
): string | null {
  if (!record) return fallback;
  for (const k of keys) {
    const raw = record[k];
    if (typeof raw === "string" && raw.length > 0) return raw;
  }
  return fallback;
}

/* -------------------------------------------------------------------------- */
/* 数字格式化                                                                  */
/* -------------------------------------------------------------------------- */

export type Currency = "CNY" | "USD";

const CURRENCY_SYMBOL: Record<Currency, string> = { CNY: "¥", USD: "$" };

/** 金额：¥12,345.67；sign=true 时正数带 +；digits 默认 2 */
export function formatMoney(
  v: number | string | null | undefined,
  currency: Currency = "CNY",
  opts: { sign?: boolean; digits?: number } = {},
): string {
  const n = typeof v === "string" ? Number(v) : v;
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const digits = opts.digits ?? 2;
  const abs = Math.abs(n);
  const body = abs.toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
  const sign = n < 0 ? "-" : opts.sign && n > 0 ? "+" : "";
  return `${sign}${CURRENCY_SYMBOL[currency]}${body}`;
}

/** 大数缩写（中文单位）：¥1.23亿 / 45.6万 —— 用于坐标轴与紧凑卡 */
export function formatCompact(
  v: number | string | null | undefined,
  currency?: Currency,
): string {
  const n = typeof v === "string" ? Number(v) : v;
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  const trim = (x: number) => String(Number(x.toFixed(2)));
  const core =
    abs >= 1e8
      ? `${trim(abs / 1e8)}亿`
      : abs >= 1e4
        ? `${trim(abs / 1e4)}万`
        : trim(abs);
  return currency ? `${sign}${CURRENCY_SYMBOL[currency]}${core}` : `${sign}${core}`;
}

/** 百分比：+12.34%（输入 12.34） */
export function formatPct(
  v: number | string | null | undefined,
  opts: { sign?: boolean; digits?: number } = {},
): string {
  const n = typeof v === "string" ? Number(v) : v;
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const digits = opts.digits ?? 2;
  const sign = n > 0 && opts.sign !== false ? "+" : "";
  return `${sign}${n.toFixed(digits)}%`;
}

/** BTC 数量：₿0.12345678（8 位小数） */
export function formatBtc(v: number | string | null | undefined): string {
  const n = typeof v === "string" ? Number(v) : v;
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `₿${n.toFixed(8)}`;
}

/* -------------------------------------------------------------------------- */
/* 日期格式化                                                                  */
/* -------------------------------------------------------------------------- */

/** yyyy-MM-dd（非法输入原样返回字符串或占位） */
export function formatDate(v: string | number | Date | null | undefined): string {
  if (v === null || v === undefined || v === "") return "—";
  const d = v instanceof Date ? v : new Date(v);
  if (Number.isNaN(d.getTime())) return typeof v === "string" ? v : "—";
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

/** yyyy-MM-dd HH:mm */
export function formatDateTime(v: string | number | Date | null | undefined): string {
  if (v === null || v === undefined || v === "") return "—";
  const d = v instanceof Date ? v : new Date(v);
  if (Number.isNaN(d.getTime())) return typeof v === "string" ? v : "—";
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${formatDate(d)} ${hh}:${mm}`;
}

/** 今天（本地时区 yyyy-MM-dd） */
export function todayISO(): string {
  return formatDate(new Date());
}

/** ISO 日期字符串加 N 天（用于回放未来窗口边界） */
export function addDaysISO(dateISO: string, days: number): string {
  const d = new Date(`${dateISO}T00:00:00`);
  if (Number.isNaN(d.getTime())) return dateISO;
  d.setDate(d.getDate() + days);
  return formatDate(d);
}

/* -------------------------------------------------------------------------- */
/* 业务语义映射                                                                 */
/* -------------------------------------------------------------------------- */

/** 计划状态徽标样式 */
export function planStatusMeta(status?: string | null): {
  label: string;
  className: string;
  bar: string;
} {
  switch ((status ?? "").toUpperCase()) {
    case "ACTIVE":
      return { label: "进行中", className: "border-up/40 bg-up/10 text-up", bar: "bg-up" };
    case "PAUSED":
      return { label: "已暂停", className: "border-warn/40 bg-warn/10 text-warn", bar: "bg-warn" };
    case "COMPLETED":
      return {
        label: "已完成",
        className: "border-accent/40 bg-accent/10 text-accent",
        bar: "bg-accent",
      };
    case "CANCELLED":
      return {
        label: "已取消",
        className: "border-muted/40 bg-muted/10 text-muted",
        bar: "bg-muted",
      };
    default:
      return {
        label: status || "未知",
        className: "border-line bg-bg-hover text-muted",
        bar: "bg-line",
      };
  }
}

/** 定投频率中文 */
export function frequencyLabel(f?: string | null): string {
  switch ((f ?? "").toLowerCase()) {
    case "daily":
      return "每日";
    case "weekly":
      return "每周";
    case "biweekly":
      return "双周";
    case "monthly":
      return "每月";
    default:
      return f || "—";
  }
}

/** 策略模板中文 */
export function strategyLabel(code?: string | null): string {
  const c = (code ?? "").toLowerCase();
  if (["fixed", "fixed_dca", "dca"].includes(c)) return "固定定投";
  if (["dip_ath", "drawdown_dca", "dip_buyer"].includes(c)) return "下跌加仓";
  if (["dip_recent", "recent_drawdown"].includes(c)) return "回撤加仓";
  if (["value", "value_dca", "valuation"].includes(c)) return "估值加仓";
  if (["risk_adjust", "risk_parity"].includes(c)) return "风险调整";
  if (["cycle_adjust"].includes(c)) return "周期调整";
  if (["custom"].includes(c)) return "自定义";
  return code || "—";
}

/** 交易方向徽标 */
export function sideMeta(side?: string | null): { label: string; className: string } {
  return (side ?? "").toUpperCase() === "SELL"
    ? { label: "卖出", className: "border-down/50 bg-down/10 text-down" }
    : { label: "买入", className: "border-up/40 bg-up/10 text-up" };
}

/** 引擎状态 → 中文（未收录的返回原文） */
const ENGINE_STATE_LABELS: Record<string, string> = {
  // 周期
  ACCUMULATION: "吸筹期",
  BOTTOM_BUILDING: "底部构筑",
  RECOVERY: "恢复期",
  UPTREND: "趋势上涨",
  ACCELERATION: "加速上涨",
  EUPHORIA: "亢奋期",
  DISTRIBUTION: "高位分配",
  TOP_RISK: "顶部风险",
  DOWNTREND: "下跌趋势",
  BEAR: "熊市",
  DEEP_BEAR: "深度熊市",
  BULL: "牛市",
  // 估值
  DEEP_UNDERVALUE: "深度低估",
  UNDERVALUE: "低估",
  FAIR: "合理",
  HIGH: "偏高",
  EXTREME_HIGH: "极端高估",
  OVERVALUE: "高估",
  EXTREME_OVERVALUE: "极端高估",
  // 风险
  VERY_LOW: "极低",
  LOW: "低",
  MODERATE: "中等",
  MEDIUM: "中等",
  EXTREME: "极端",
  VERY_HIGH: "极高",
  // Regime 趋势/方向
  BULLISH: "看多",
  BEARISH: "看空",
  NEUTRAL: "中性",
};

/** 状态语义色（用于状态文字） */
export function stateTone(state?: string | null): "up" | "down" | "warn" | "accent" | "muted" {
  const s = (state ?? "").toUpperCase();
  if (["DEEP_UNDERVALUE", "UNDERVALUE", "LOW", "VERY_LOW", "BULLISH", "UPTREND", "ACCUMULATION", "RECOVERY", "BOTTOM_BUILDING"].includes(s)) return "up";
  if (["EXTREME_HIGH", "EXTREME_OVERVALUE", "EXTREME", "VERY_HIGH", "BEARISH", "TOP_RISK", "DOWNTREND", "DEEP_BEAR"].includes(s)) return "down";
  if (["HIGH", "OVERVALUE", "MEDIUM", "MODERATE", "EUPHORIA", "ACCELERATION"].includes(s)) return "warn";
  if (["FAIR", "NEUTRAL"].includes(s)) return "accent";
  return "muted";
}

export function translateState(state?: string | null): string {
  if (!state) return "—";
  const upper = state.toUpperCase();
  return ENGINE_STATE_LABELS[upper] ?? state;
}

/** 引擎中文名 */
export function engineLabel(engine: string): string {
  switch (engine) {
    case "cycle":
      return "市场周期";
    case "valuation":
      return "估值";
    case "risk":
      return "风险";
    case "regime":
      return "综合状态";
    default:
      return engine;
  }
}
