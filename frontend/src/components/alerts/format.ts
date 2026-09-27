/**
 * 智能预警模块格式化工具（时间 / 数值 / 时长）。
 */

/** ISO -> 本地时间字符串（无参返回 "—"） */
export function formatDateTime(iso?: string | number | Date | null, withSeconds = false): string {
  if (iso === null || iso === undefined || iso === "") return "—";
  const d = iso instanceof Date ? iso : new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    ...(withSeconds ? { second: "2-digit" } : {}),
    hour12: false,
  });
}

/** ISO -> yyyy-MM-dd（用于 date input 默认值） */
export function formatDateInput(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

/** ISO -> 日期部分（仅展示） */
export function formatDateOnly(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

/** 相对时间（"3 小时前"），超过 30 天回退为绝对日期 */
export function formatRelative(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const diffMs = Date.now() - d.getTime();
  if (diffMs < 0) return formatDateTime(iso);
  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days <= 30) return `${days} 天前`;
  return formatDateOnly(iso);
}

/** 数值展示：大数加千分位、小数去尾零；空值 "—" */
export function formatNumber(v?: number | null, digits?: number): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const abs = Math.abs(v);
  let text: string;
  if (digits !== undefined) {
    text = v.toFixed(digits);
  } else if (abs >= 1000) {
    text = v.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  } else if (abs >= 1) {
    text = String(Math.round(v * 100) / 100);
  } else if (abs === 0) {
    text = "0";
  } else {
    // 小数自适应（最多 6 位）
    text = v.toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
  }
  return text;
}

/** 带符号百分比（+12.3% / -4.5%） */
export function formatSignedPct(v?: number | null, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

/** 秒数 -> 人类可读时长（"10 分钟" / "1.5 小时" / "3 天"） */
export function formatDuration(seconds?: number | null): string {
  if (seconds === null || seconds === undefined) return "不限";
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = seconds / 60;
  if (minutes < 60) return `${minutes % 1 === 0 ? minutes : minutes.toFixed(1)} 分钟`;
  const hours = seconds / 3600;
  if (hours < 24) return `${hours % 1 === 0 ? hours : hours.toFixed(1)} 小时`;
  const days = seconds / 86400;
  return `${days % 1 === 0 ? days : days.toFixed(1)} 天`;
}
