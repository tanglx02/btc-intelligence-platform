/**
 * 涨跌指示器：▲ 绿 / ▼ 红 + 百分比（等宽数字对齐）。
 * 默认遵循中国习惯：涨绿跌红。
 */

interface TrendIndicatorProps {
  /** 涨跌幅百分数，如 2.31 表示 +2.31%；null/undefined 显示占位 */
  value?: number | null;
  /** 后缀，默认 % */
  suffix?: string;
  /** 是否显示正号 */
  showSign?: boolean;
  size?: "xs" | "sm" | "md" | "lg";
  className?: string;
}

const SIZE_CLASS: Record<NonNullable<TrendIndicatorProps["size"]>, string> = {
  xs: "text-[11px]",
  sm: "text-xs",
  md: "text-sm",
  lg: "text-base",
};

export function TrendIndicator({
  value,
  suffix = "%",
  showSign = true,
  size = "sm",
  className = "",
}: TrendIndicatorProps) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return (
      <span className={`font-mono tabular-nums text-muted ${SIZE_CLASS[size]} ${className}`}>
        —
      </span>
    );
  }

  const positive = value > 0;
  const negative = value < 0;
  const color = positive ? "text-up" : negative ? "text-down" : "text-muted";
  const arrow = positive ? "▲" : negative ? "▼" : "—";
  const sign = positive && showSign ? "+" : "";

  return (
    <span className={`inline-flex items-center gap-0.5 font-mono tabular-nums ${SIZE_CLASS[size]} ${color} ${className}`}>
      <span aria-hidden className="text-[0.8em] leading-none">
        {arrow}
      </span>
      <span>
        {sign}
        {value.toFixed(2)}
        {suffix}
      </span>
    </span>
  );
}
