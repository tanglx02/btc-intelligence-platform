"use client";

/**
 * 情绪页：
 * - 恐惧贪婪指数半圆仪表（ECharts gauge，红->绿色带）
 * - 近 180 天历史曲线（按数值区间着色）
 * - 当前档位说明文字
 */

import { useMemo } from "react";

import { DataSourceBadge } from "@/components/common/DataSourceBadge";
import { ErrorState } from "@/components/common/ErrorState";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";
import { QualityBadge } from "@/components/common/QualityBadge";
import { EChart, PageHeader, SectionTitle } from "@/components/market/kit";
import { useApi } from "@/hooks/useApi";
import {
  baseTooltip,
  CHART,
  fgBand,
  fgClassLabel,
  pickNumber,
  pickString,
  shortDate,
} from "@/components/market/utils";
import { apiClient } from "@/lib/api";
import type { FearGreedIndex } from "@/types/api";
import type { EChartsOption } from "echarts";

interface FgPoint {
  date: string;
  value: number;
}

/** 历史序列：兼容数组直出 / { history: [...] } 包裹 */
function parseFgHistory(raw: unknown): FgPoint[] {
  let rows: unknown[] = [];
  if (Array.isArray(raw)) rows = raw;
  else if (raw && typeof raw === "object") {
    const o = raw as Record<string, unknown>;
    for (const k of ["history", "items", "rows", "data", "series"]) {
      if (Array.isArray(o[k])) {
        rows = o[k] as unknown[];
        break;
      }
    }
  }
  const out: FgPoint[] = [];
  for (const row of rows) {
    const value = pickNumber(row, ["value", "score", "fg", "fear_greed"]);
    if (value === null) continue;
    const date = shortDate(pickString(row, ["date", "time", "updated_at", "day"])) ?? "";
    out.push({ date, value: Math.max(0, Math.min(100, value)) });
  }
  return out;
}

/** gauge 色带与 FG_BANDS 对齐 */
const GAUGE_COLORS: [number, string][] = [
  [0.24, "#f85149"],
  [0.44, "#db6d28"],
  [0.55, "#d29922"],
  [0.75, "#56d364"],
  [1, "#3fb950"],
];

export default function SentimentPage() {
  const latestApi = useApi<FearGreedIndex>(() => apiClient.sentiment.getFearGreed({ limit: 1 }), [], {
    refreshInterval: 300_000,
  });
  const historyApi = useApi<unknown>(() => apiClient.sentiment.getFearGreed({ limit: 180 }), [], {
    refreshInterval: 600_000,
  });

  const latest = latestApi.data;
  const value = pickNumber(latest, ["value", "score"]);
  const classLabel = fgClassLabel(pickString(latest, ["classification", "label", "category"]), value);
  const prevValue = pickNumber(latest, ["previous_value", "previous", "prev_value"]);
  const updatedAt = pickString(latest, ["updated_at", "date", "time"]);
  const band = value !== null ? fgBand(value) : null;

  const history = useMemo(() => {
    const parsed = parseFgHistory(historyApi.data);
    if (parsed.length > 0) return parsed;
    // 兜底：最新响应里自带 history 字段
    return parseFgHistory(latest);
  }, [historyApi.data, latest]);

  // gauge option 直接内联构建：依赖链源自 API 响应派生值，React Compiler 无法保留手动 memo
  const gaugeOption: EChartsOption = {
    series: [
      {
        type: "gauge",
        startAngle: 180,
        endAngle: 0,
        min: 0,
        max: 100,
        radius: "100%",
        center: ["50%", "78%"],
        splitNumber: 5,
        axisLine: { lineStyle: { width: 20, color: GAUGE_COLORS } },
        pointer: {
          icon: "triangle",
          length: "58%",
          width: 7,
          offsetCenter: [0, "-4%"],
          itemStyle: { color: "#e6edf3" },
        },
        axisTick: { distance: -22, length: 4, lineStyle: { color: "#0d1117", width: 1 } },
        splitLine: { distance: -24, length: 20, lineStyle: { color: "#0d1117", width: 2 } },
        axisLabel: { distance: -38, color: CHART.text, fontSize: 10 },
        anchor: { show: true, size: 10, itemStyle: { color: "#e6edf3" } },
        detail: {
          valueAnimation: true,
          formatter: value !== null ? `${value}` : "—",
          color: band?.color ?? CHART.muted,
          fontSize: 34,
          fontWeight: "bold",
          offsetCenter: [0, "22%"],
        },
        title: { show: false },
        data: [{ value: value ?? 0 }],
      },
    ],
  };

  const historyOption: EChartsOption = useMemo(
    () => ({
      tooltip: {
        trigger: "axis",
        ...baseTooltip(),
        valueFormatter: (v) => (typeof v === "number" ? v.toFixed(0) : "—"),
      },
      visualMap: {
        show: false,
        pieces: [
          { lt: 24, color: "#f85149" },
          { gte: 24, lt: 44, color: "#db6d28" },
          { gte: 44, lt: 55, color: "#d29922" },
          { gte: 55, lt: 75, color: "#56d364" },
          { gte: 75, color: "#3fb950" },
        ],
      },
      grid: { left: 8, right: 12, top: 20, bottom: 0, containLabel: true },
      xAxis: {
        type: "category",
        data: history.map((p) => p.date),
        boundaryGap: false,
        axisLine: { lineStyle: { color: CHART.axis } },
        axisTick: { show: false },
        axisLabel: { color: CHART.text, fontSize: 10 },
      },
      yAxis: {
        type: "value",
        min: 0,
        max: 100,
        splitLine: { lineStyle: { color: CHART.grid } },
        axisLabel: { color: CHART.text, fontSize: 10 },
      },
      series: [
        {
          type: "line",
          data: history.map((p) => p.value),
          showSymbol: false,
          lineStyle: { width: 1.6 },
          markLine: {
            silent: true,
            symbol: "none",
            lineStyle: { color: CHART.muted, type: "dashed", width: 1 },
            label: { show: false },
            data: [{ yAxis: 50 }],
          },
        },
      ],
    }),
    [history],
  );

  const hasAnyData = latest !== null || history.length > 0;

  return (
    <div>
      <PageHeader
        title="市场情绪"
        subtitle="恐惧贪婪指数 · 综合波动率、动量、社交与市场占比的情绪合成读数（0-100）"
        right={
          <>
            <QualityBadge status={latestApi.meta?.quality_status ?? null} />
            <DataSourceBadge source={latestApi.meta?.source} updatedAt={latestApi.meta?.timestamp} />
          </>
        }
      />

      {latestApi.error && !hasAnyData && (
        <ErrorState
          error={latestApi.error ?? historyApi.error}
          lastUpdatedAt={latestApi.lastUpdatedAt ?? historyApi.lastUpdatedAt}
          onRetry={() => {
            latestApi.refresh();
            historyApi.refresh();
          }}
          className="mb-4"
        />
      )}

      {latestApi.loading && !hasAnyData ? (
        <LoadingSkeleton variant="card" />
      ) : (
        <div className="space-y-5">
          {/* 仪表 + 说明 */}
          <section className="grid gap-4 lg:grid-cols-5">
            <div className="overflow-hidden rounded-xl border border-line bg-bg-raised p-4 lg:col-span-3">
              {value !== null ? (
                <EChart option={gaugeOption} height={280} />
              ) : (
                <div className="flex h-[280px] items-center justify-center text-xs text-muted">
                  恐惧贪婪数据暂不可用（{latestApi.error?.message ?? "暂无数据"}）
                </div>
              )}
            </div>

            <div className="flex flex-col justify-center rounded-xl border border-line bg-bg-raised p-5 lg:col-span-2">
              <p className="font-mono text-[10px] uppercase tracking-[0.24em] text-muted">
                当前读数
              </p>
              <p className="mt-2 flex items-baseline gap-3">
                <span
                  className="text-4xl font-bold tabular-nums"
                  style={{ color: band?.color ?? CHART.muted }}
                >
                  {value !== null ? Math.round(value) : "—"}
                </span>
                <span className="text-lg font-semibold text-[#e6edf3]">{classLabel}</span>
              </p>
              {value !== null && prevValue !== null && prevValue !== value && (
                <p className="mt-1 text-xs text-muted">
                  较前一读数{" "}
                  <span className={value > prevValue ? "text-up" : "text-down"}>
                    {value > prevValue ? "+" : ""}
                    {Math.round(value - prevValue)}
                  </span>
                  {updatedAt ? ` · ${updatedAt.slice(0, 10)}` : ""}
                </p>
              )}
              {band && (
                <p className="mt-4 text-xs leading-relaxed text-[#c9d1d9]">{band.desc}</p>
              )}
              <p className="mt-3 text-[11px] leading-relaxed text-muted">
                读数越低代表市场越恐惧（常与下跌/恐慌相伴），越高代表越贪婪（常与过热相伴）。
                情绪指标适合观察市场状态，不构成方向预测。
              </p>
            </div>
          </section>

          {/* 历史曲线 */}
          {history.length > 0 && (
            <section className="rounded-xl border border-line bg-bg-raised p-4">
              <SectionTitle right={<span className="text-[10px] text-muted">虚线为 50 中性位</span>}>
                近 180 天历史
              </SectionTitle>
              <EChart option={historyOption} height={260} />
            </section>
          )}
        </div>
      )}
    </div>
  );
}
