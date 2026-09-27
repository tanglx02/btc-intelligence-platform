"use client";

/**
 * 个人页面组图表封装：ECharts 动态导入（ssr:false）。
 *
 * - EquityCurveChart  资产/净值曲线（市值 vs 累计投入双线，市值带面积渐变）
 * - DrawdownChart     回撤水下图
 * - MultiLineChart    多序列叠加（策略对比）
 * - PricePathChart    价格路径（历史回放事后视角）
 *
 * 主题：暗色金融终端（#0d1117），坐标轴 #30363d，文字 #8b949e。
 */

import dynamic from "next/dynamic";
import { useMemo } from "react";

import { formatCompact, formatMoney, type Currency } from "../utils";

const EChartView = dynamic(() => import("./EChartView"), {
  ssr: false,
  loading: () => (
    <div className="flex h-full min-h-40 items-center justify-center text-xs text-muted">
      图表加载中…
    </div>
  ),
});

/* -------------------------------------------------------------------------- */
/* 主题常量                                                                    */
/* -------------------------------------------------------------------------- */

const COLOR = {
  accent: "#58a6ff",
  up: "#3fb950",
  down: "#f85149",
  warn: "#d29922",
  orange: "#db6d28",
  purple: "#bc8cff",
  teal: "#39d2c0",
  muted: "#8b949e",
  line: "#30363d",
  split: "#21262d",
  fg: "#e6edf3",
} as const;

export const SERIES_PALETTE = [
  COLOR.accent,
  COLOR.up,
  COLOR.warn,
  COLOR.purple,
  COLOR.teal,
  COLOR.orange,
];

export interface CurvePoint {
  date: string;
  value: number;
  invested?: number;
}

const AXIS_STYLE = {
  axisLine: { lineStyle: { color: COLOR.line } },
  axisLabel: { color: COLOR.muted, fontSize: 10 },
  axisTick: { show: false },
} as const;

function baseGrid() {
  return { left: 8, right: 12, top: 28, bottom: 6, containLabel: true };
}

function baseTooltip(currency?: Currency, extraFormatter?: (v: number) => string) {
  return {
    trigger: "axis" as const,
    backgroundColor: "rgba(22,27,34,0.96)",
    borderColor: COLOR.line,
    borderWidth: 1,
    padding: [8, 12],
    textStyle: { color: COLOR.fg, fontSize: 11 },
    valueFormatter: (v: unknown) =>
      typeof v === "number"
        ? extraFormatter
          ? extraFormatter(v)
          : currency
            ? formatMoney(v, currency)
            : String(v)
        : typeof v === "string"
          ? v
          : "—",
  };
}

function ChartFrame({
  option,
  height,
  className,
}: {
  option: Record<string, unknown>;
  height: number;
  className?: string;
}) {
  return (
    <div className={className} style={{ height }}>
      <EChartView option={option} height={height} />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 资产曲线（市值 vs 累计投入）                                                 */
/* -------------------------------------------------------------------------- */

export function EquityCurveChart({
  data,
  currency = "CNY",
  height = 320,
  valueName = "当前市值",
  investedName = "累计投入",
  valueColor = COLOR.accent,
  className = "",
}: {
  data: CurvePoint[];
  currency?: Currency;
  height?: number;
  valueName?: string;
  investedName?: string;
  valueColor?: string;
  className?: string;
}) {
  const option = useMemo(() => {
    const dates = data.map((d) => d.date);
    const hasInvested = data.some((d) => typeof d.invested === "number");
    return {
      animationDuration: 500,
      grid: baseGrid(),
      legend: {
        show: hasInvested,
        top: 0,
        right: 0,
        icon: "roundRect",
        itemWidth: 10,
        itemHeight: 3,
        textStyle: { color: COLOR.muted, fontSize: 10 },
      },
      tooltip: baseTooltip(currency),
      xAxis: {
        type: "category" as const,
        data: dates,
        boundaryGap: false,
        ...AXIS_STYLE,
      },
      yAxis: {
        type: "value" as const,
        splitLine: { lineStyle: { color: COLOR.split, type: "dashed" as const } },
        axisLabel: {
          color: COLOR.muted,
          fontSize: 10,
          formatter: (v: number) => formatCompact(v, currency),
        },
      },
      dataZoom: data.length > 200
        ? [{ type: "inside" as const }, { type: "slider" as const, height: 16, bottom: 2 }]
        : [{ type: "inside" as const }],
      series: [
        {
          name: valueName,
          type: "line" as const,
          data: data.map((d) => d.value),
          symbol: "none",
          lineStyle: { width: 1.8, color: valueColor },
          itemStyle: { color: valueColor },
          areaStyle: {
            color: {
              type: "linear" as const,
              x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [
                { offset: 0, color: `${valueColor}33` },
                { offset: 1, color: `${valueColor}00` },
              ],
            },
          },
          emphasis: { focus: "series" as const },
        },
        ...(hasInvested
          ? [
              {
                name: investedName,
                type: "line" as const,
                data: data.map((d) => d.invested ?? null),
                symbol: "none",
                lineStyle: { width: 1.2, color: COLOR.muted, type: "dashed" as const },
                itemStyle: { color: COLOR.muted },
                emphasis: { focus: "series" as const },
              },
            ]
          : []),
      ],
    };
  }, [data, currency, valueName, investedName, valueColor]);

  return <ChartFrame option={option} height={height} className={className} />;
}

/* -------------------------------------------------------------------------- */
/* 回撤水下图                                                                  */
/* -------------------------------------------------------------------------- */

export function DrawdownChart({
  data,
  height = 180,
  className = "",
}: {
  data: { date: string; drawdown_pct: number }[];
  height?: number;
  className?: string;
}) {
  const option = useMemo(
    () => ({
      animationDuration: 500,
      grid: { ...baseGrid(), top: 8 },
      tooltip: baseTooltip(undefined, (v) => `${v.toFixed(2)}%`),
      xAxis: {
        type: "category" as const,
        data: data.map((d) => d.date),
        boundaryGap: false,
        ...AXIS_STYLE,
      },
      yAxis: {
        type: "value" as const,
        max: 0,
        splitLine: { lineStyle: { color: COLOR.split, type: "dashed" as const } },
        axisLabel: { color: COLOR.muted, fontSize: 10, formatter: (v: number) => `${v}%` },
      },
      dataZoom: data.length > 200
        ? [{ type: "inside" as const }, { type: "slider" as const, height: 16, bottom: 2 }]
        : [{ type: "inside" as const }],
      series: [
        {
          name: "回撤",
          type: "line" as const,
          data: data.map((d) => d.drawdown_pct),
          symbol: "none",
          lineStyle: { width: 1.2, color: COLOR.down },
          itemStyle: { color: COLOR.down },
          areaStyle: {
            color: {
              type: "linear" as const,
              x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [
                { offset: 0, color: "rgba(248,81,73,0.02)" },
                { offset: 1, color: "rgba(248,81,73,0.35)" },
              ],
            },
          },
        },
      ],
    }),
    [data],
  );

  return <ChartFrame option={option} height={height} className={className} />;
}

/* -------------------------------------------------------------------------- */
/* 多序列对比图                                                                */
/* -------------------------------------------------------------------------- */

export interface MultiSeries {
  name: string;
  points: { date: string; value: number }[];
  color?: string;
  dashed?: boolean;
}

export function MultiLineChart({
  series,
  currency,
  height = 320,
  yPercent = false,
  className = "",
}: {
  series: MultiSeries[];
  currency?: Currency;
  height?: number;
  yPercent?: boolean;
  className?: string;
}) {
  const option = useMemo(() => {
    // 以最长序列的日期轴为基准（对比区间一致）
    const base = series.reduce(
      (acc, s) => (s.points.length > acc.length ? s.points : acc),
      [] as { date: string; value: number }[],
    );
    const dates = base.map((p) => p.date);
    const formatter = yPercent
      ? (v: number) => `${v.toFixed(1)}%`
      : currency
        ? (v: number) => formatCompact(v, currency)
        : (v: number) => formatCompact(v);

    return {
      animationDuration: 500,
      grid: baseGrid(),
      legend: {
        top: 0,
        right: 0,
        icon: "roundRect",
        itemWidth: 10,
        itemHeight: 3,
        textStyle: { color: COLOR.muted, fontSize: 10 },
      },
      tooltip: baseTooltip(yPercent ? undefined : currency, yPercent ? (v) => `${v.toFixed(2)}%` : undefined),
      xAxis: { type: "category" as const, data: dates, boundaryGap: false, ...AXIS_STYLE },
      yAxis: {
        type: "value" as const,
        scale: true,
        splitLine: { lineStyle: { color: COLOR.split, type: "dashed" as const } },
        axisLabel: { color: COLOR.muted, fontSize: 10, formatter: formatter },
      },
      dataZoom: dates.length > 200
        ? [{ type: "inside" as const }, { type: "slider" as const, height: 16, bottom: 2 }]
        : [{ type: "inside" as const }],
      series: series.map((s, i) => ({
        name: s.name,
        type: "line" as const,
        data: dates.map((d) => {
          const hit = s.points.find((p) => p.date === d);
          return hit ? hit.value : null;
        }),
        symbol: "none",
        lineStyle: {
          width: 1.6,
          color: s.color ?? SERIES_PALETTE[i % SERIES_PALETTE.length],
          ...(s.dashed ? { type: "dashed" as const } : {}),
        },
        itemStyle: { color: s.color ?? SERIES_PALETTE[i % SERIES_PALETTE.length] },
        emphasis: { focus: "series" as const },
      })),
    };
  }, [series, currency, yPercent]);

  return <ChartFrame option={option} height={height} className={className} />;
}

/* -------------------------------------------------------------------------- */
/* 价格路径（历史回放 · 事后视角）                                              */
/* -------------------------------------------------------------------------- */

export function PricePathChart({
  points,
  height = 280,
  windowMarks = [],
  className = "",
}: {
  points: { date: string; close: number }[];
  height?: number;
  /** 未来窗口分界标注（天） */
  windowMarks?: number[];
  className?: string;
}) {
  const option = useMemo(() => {
    const dates = points.map((p) => p.date);
    const closes = points.map((p) => p.close);
    const startIndex = 0;
    const marks = windowMarks
      .map((d) => {
        const idx = Math.min(d, dates.length - 1);
        if (idx < 0 || !dates.length) return null;
        return {
          xAxis: dates[idx],
          lineStyle: { color: COLOR.warn, type: "dashed" as const, width: 1 },
          label: {
            formatter: `${d}天`,
            color: COLOR.warn,
            fontSize: 10,
            position: "insideEndTop" as const,
          },
          symbol: "none" as const,
        };
      })
      .filter(Boolean);

    return {
      animationDuration: 500,
      grid: baseGrid(),
      tooltip: baseTooltip("USD"),
      xAxis: { type: "category" as const, data: dates, boundaryGap: false, ...AXIS_STYLE },
      yAxis: {
        type: "value" as const,
        scale: true,
        splitLine: { lineStyle: { color: COLOR.split, type: "dashed" as const } },
        axisLabel: { color: COLOR.muted, fontSize: 10, formatter: (v: number) => formatCompact(v, "USD") },
      },
      series: [
        {
          name: "BTC 收盘价",
          type: "line" as const,
          data: closes,
          symbol: "none",
          lineStyle: { width: 1.8, color: COLOR.accent },
          itemStyle: { color: COLOR.accent },
          areaStyle: {
            color: {
              type: "linear" as const,
              x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [
                { offset: 0, color: "rgba(88,166,255,0.20)" },
                { offset: 1, color: "rgba(88,166,255,0)" },
              ],
            },
          },
          markLine: marks.length ? { silent: true, data: marks } : undefined,
        },
      ],
      // 基准点索引用于后续扩展
      _startIndex: startIndex,
    };
  }, [points, windowMarks]);

  return <ChartFrame option={option} height={height} className={className} />;
}
