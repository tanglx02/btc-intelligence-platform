"use client";

/**
 * 链上页：
 * - 链上指标卡网格（当前值 + 历史分位 + QualityBadge）
 * - 点击卡片切换 ECharts 历史曲线
 * - 交易所净流（USD / 日，正绿负红）
 */

import { useMemo, useState } from "react";

import { ErrorState } from "@/components/common/ErrorState";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";
import { QualityBadge } from "@/components/common/QualityBadge";
import { EChart, PageHeader, SectionTitle } from "@/components/market/kit";
import { useApi } from "@/hooks/useApi";
import {
  baseTooltip,
  CHART,
  daysAgoISO,
  extractSeriesRecords,
  formatNumber,
  formatUsd,
  pickNumber,
  pickString,
  shortDate,
} from "@/components/market/utils";
import { ApiRequestError, apiClient } from "@/lib/api";
import type { ApiResponse, OnchainMetricPoint, QualityStatus } from "@/types/api";
import type { EChartsOption } from "echarts";

const RANGE_DAYS = 730;

interface MetricDef {
  key: string;
  label: string;
  desc: string;
  /** ratio: 固定 3 位小数比率；count: 大数缩写整数 */
  fmt: "ratio" | "count";
}

const METRIC_DEFS: MetricDef[] = [
  { key: "mvrv", label: "MVRV", desc: "市值 / 已实现市值，大于 1 表示整体持仓处于浮盈", fmt: "ratio" },
  { key: "sopr", label: "SOPR", desc: "链上支出的实现利润率，大于 1 表示整体盈利卖出", fmt: "ratio" },
  { key: "nupl", label: "NUPL", desc: "未实现净利润占市值比例", fmt: "ratio" },
  { key: "puell_multiple", label: "Puell Multiple", desc: "矿工日发行收入相对年均水平的倍数", fmt: "ratio" },
  { key: "active_addresses", label: "活跃地址", desc: "链上每日活跃地址数量", fmt: "count" },
];

const QUALITY_VALUES: readonly string[] = ["VERIFIED", "ESTIMATED", "STALE", "CONFLICT", "INVALID"];

/** 宽松提取单点的质量状态（点本体或其 metadata） */
function qualityOf(raw: unknown): QualityStatus | null {
  if (Array.isArray(raw)) {
    if (raw.length === 0) return null;
    return qualityOf(raw[raw.length - 1]);
  }
  if (!raw || typeof raw !== "object") return null;
  const o = raw as Record<string, unknown>;
  for (const c of [o, o.metadata]) {
    const q = pickString(c, ["quality_status", "quality"]);
    if (q && QUALITY_VALUES.includes(q.toUpperCase())) return q.toUpperCase() as QualityStatus;
  }
  return null;
}

/** 当前值在序列中的历史分位（0-100） */
function percentileOf(series: { value: number }[], current: number | null): number | null {
  if (current === null) return null;
  const values = series.map((p) => p.value).filter((v) => Number.isFinite(v));
  if (values.length < 10) return null;
  const below = values.filter((v) => v <= current).length;
  return (below / values.length) * 100;
}

interface FlowDay {
  date: string;
  net: number;
}

/** 交易所净流行数据（后端为行数组，兼容聚合包裹结构），按日聚合 */
function parseFlowDays(raw: unknown): FlowDay[] {
  let rows: unknown[] = [];
  if (Array.isArray(raw)) rows = raw;
  else if (raw && typeof raw === "object") {
    const o = raw as Record<string, unknown>;
    for (const k of ["history", "rows", "items", "data"]) {
      if (Array.isArray(o[k])) {
        rows = o[k] as unknown[];
        break;
      }
    }
  }
  const byDate = new Map<string, number>();
  for (const row of rows) {
    if (!row || typeof row !== "object") continue;
    const date = shortDate(pickString(row, ["observation_time", "date", "time", "day"]));
    if (!date) continue;
    const net = pickNumber(row, ["net_flow_usd", "netflow_usd", "net_flow", "netflow"]);
    if (net === null) continue;
    byDate.set(date, (byDate.get(date) ?? 0) + net);
  }
  return [...byDate.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([date, net]) => ({ date, net }));
}

function fmtMetricValue(v: number | null, fmt: MetricDef["fmt"]): string {
  if (v === null) return "—";
  return fmt === "count" ? formatNumber(v, 0) : v.toFixed(3);
}

export default function OnchainPage() {
  // 五个链上指标批量拉取；单个指标失败不影响其余
  const metricsApi = useApi<Record<string, OnchainMetricPoint[]>>(
    async () => {
      let failed = 0;
      const entries = await Promise.all(
        METRIC_DEFS.map(async (m) => {
          try {
            const res = await apiClient.onchain.getMetricHistory(m.key, {
              start: daysAgoISO(RANGE_DAYS),
              end: daysAgoISO(0),
            });
            return [m.key, res.data ?? []] as const;
          } catch {
            failed += 1;
            return [m.key, []] as const;
          }
        }),
      );
      if (failed === METRIC_DEFS.length) {
        throw new ApiRequestError("链上指标服务不可达", { code: "NETWORK_ERROR" });
      }
      return {
        success: true,
        data: Object.fromEntries(entries),
        meta: null,
      } as ApiResponse<Record<string, OnchainMetricPoint[]>>;
    },
    [],
    { refreshInterval: 600_000 },
  );

  const flowApi = useApi<unknown>(
    () => apiClient.onchain.getExchangeFlow({ start: daysAgoISO(90), end: daysAgoISO(0) }),
    [],
    { refreshInterval: 300_000 },
  );

  const [active, setActive] = useState("mvrv");

  const metricCards = useMemo(() => {
    const data = metricsApi.data ?? {};
    return METRIC_DEFS.map((def) => {
      const raw = data[def.key] ?? null;
      const series = extractSeriesRecords(raw);
      const current = series.length > 0 ? series[series.length - 1].value : null;
      return {
        def,
        series,
        current,
        percentile: percentileOf(series, current),
        updated: series.length > 0 ? series[series.length - 1].time : null,
        quality: qualityOf(raw),
      };
    });
  }, [metricsApi.data]);

  const flowDays = useMemo(() => parseFlowDays(flowApi.data), [flowApi.data]);
  const flowCurrent = flowDays.length > 0 ? flowDays[flowDays.length - 1].net : null;
  const flowPercentile = useMemo(
    () => percentileOf(flowDays.map((d) => ({ value: d.net })), flowCurrent),
    [flowDays, flowCurrent],
  );

  const activeIsFlow = active === "exchange_flow";
  const activeCard = metricCards.find((c) => c.def.key === active) ?? null;

  const chartOption: EChartsOption = useMemo(() => {
    if (activeIsFlow) {
      return {
        tooltip: {
          trigger: "axis",
          axisPointer: { type: "shadow" },
          ...baseTooltip(),
          valueFormatter: (v) => formatUsd(typeof v === "number" ? v : null),
        },
        grid: { left: 8, right: 12, top: 24, bottom: 0, containLabel: true },
        xAxis: {
          type: "category",
          data: flowDays.map((d) => d.date),
          axisLine: { lineStyle: { color: CHART.axis } },
          axisTick: { show: false },
          axisLabel: { color: CHART.text, fontSize: 10 },
        },
        yAxis: {
          type: "value",
          splitLine: { lineStyle: { color: CHART.grid } },
          axisLabel: { color: CHART.text, fontSize: 10, formatter: (v: number) => formatUsd(v, 0) },
        },
        series: [
          {
            type: "bar",
            data: flowDays.map((d) => ({
              value: d.net,
              itemStyle: { color: d.net >= 0 ? "rgba(63,185,80,0.8)" : "rgba(248,81,73,0.8)" },
            })),
            barMaxWidth: 10,
          },
        ],
      };
    }
    const card = activeCard;
    if (!card || card.series.length === 0) return {};
    return {
      tooltip: {
        trigger: "axis",
        ...baseTooltip(),
        valueFormatter: (v) => (typeof v === "number" ? v.toFixed(3) : "—"),
      },
      grid: { left: 8, right: 12, top: 24, bottom: 0, containLabel: true },
      xAxis: {
        type: "category",
        data: card.series.map((p) => (p.time ? p.time.slice(0, 10) : "")),
        boundaryGap: false,
        axisLine: { lineStyle: { color: CHART.axis } },
        axisTick: { show: false },
        axisLabel: { color: CHART.text, fontSize: 10 },
      },
      yAxis: {
        type: "value",
        scale: true,
        splitLine: { lineStyle: { color: CHART.grid } },
        axisLabel: { color: CHART.text, fontSize: 10 },
      },
      series: [
        {
          type: "line",
          data: card.series.map((p) => p.value),
          showSymbol: false,
          lineStyle: { color: CHART.accent, width: 1.6 },
          areaStyle: {
            color: {
              type: "linear",
              x: 0,
              y: 0,
              x2: 0,
              y2: 1,
              colorStops: [
                { offset: 0, color: "rgba(88,166,255,0.26)" },
                { offset: 1, color: "rgba(88,166,255,0.02)" },
              ],
            },
          },
        },
      ],
    };
  }, [activeIsFlow, activeCard, flowDays]);

  const hasMetricData = metricsApi.data !== null;

  return (
    <div>
      <PageHeader
        title="链上指标"
        subtitle="MVRV、SOPR、NUPL 等核心链上指标 · 点击卡片切换下方曲线 · 分位为相对自身近 2 年分布的位置"
      />

      {metricsApi.error && !hasMetricData && (
        <ErrorState
          error={metricsApi.error}
          lastUpdatedAt={metricsApi.lastUpdatedAt}
          onRetry={metricsApi.refresh}
          className="mb-4"
        />
      )}

      {metricsApi.loading && !hasMetricData ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 6 }, (_, i) => (
            <LoadingSkeleton key={i} variant="card" />
          ))}
        </div>
      ) : (
        <>
          {/* 指标卡网格 */}
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {metricCards.map((card) => (
              <button
                key={card.def.key}
                type="button"
                onClick={() => setActive(card.def.key)}
                aria-pressed={active === card.def.key}
                title={card.def.desc}
                className={`rounded-xl border p-4 text-left transition-colors ${
                  active === card.def.key
                    ? "border-accent/60 bg-accent/5"
                    : "border-line bg-bg-raised hover:border-accent/30"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <p className="text-[11px] font-medium uppercase tracking-wider text-muted">
                    {card.def.label}
                  </p>
                  <QualityBadge status={card.quality} />
                </div>
                <p className="mt-1.5 font-mono text-lg font-semibold tabular-nums text-[#e6edf3]">
                  {fmtMetricValue(card.current, card.def.fmt)}
                </p>
                <p className="mt-1.5 text-[11px] text-muted">
                  {card.percentile !== null ? (
                    <>
                      历史分位{" "}
                      <span
                        className={
                          card.percentile >= 80 ? "text-down" : card.percentile <= 20 ? "text-up" : "text-warn"
                        }
                      >
                        {Math.round(card.percentile)}%
                      </span>
                      {card.updated ? ` · ${card.updated}` : ""}
                    </>
                  ) : card.updated ? (
                    `更新于 ${card.updated}`
                  ) : (
                    "暂无数据"
                  )}
                </p>
              </button>
            ))}

            {/* 交易所净流卡 */}
            <button
              type="button"
              onClick={() => setActive("exchange_flow")}
              aria-pressed={activeIsFlow}
              title="交易所净流入/流出（USD）"
              className={`rounded-xl border p-4 text-left transition-colors ${
                activeIsFlow ? "border-accent/60 bg-accent/5" : "border-line bg-bg-raised hover:border-accent/30"
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <p className="text-[11px] font-medium uppercase tracking-wider text-muted">交易所净流</p>
                <QualityBadge status={flowApi.meta?.quality_status ?? null} />
              </div>
              <p
                className={`mt-1.5 font-mono text-lg font-semibold tabular-nums ${
                  flowCurrent === null ? "text-[#e6edf3]" : flowCurrent >= 0 ? "text-up" : "text-down"
                }`}
              >
                {flowCurrent !== null ? formatUsd(flowCurrent) : "—"}
              </p>
              <p className="mt-1.5 text-[11px] text-muted">
                {flowPercentile !== null
                  ? `历史分位 ${Math.round(flowPercentile)}% · 最近一日净流`
                  : "最近一日净流入(+)/流出(-)"}
              </p>
            </button>
          </div>

          {/* 曲线 */}
          <section className="mt-5 rounded-xl border border-line bg-bg-raised p-4">
            <SectionTitle
              right={
                <span className="text-[10px] text-muted">
                  {activeIsFlow
                    ? "正值=净流入（充值），负值=净流出（提币）"
                    : (activeCard?.def.desc ?? "")}
                </span>
              }
            >
              {activeIsFlow
                ? "交易所净流（近 90 天）"
                : `${activeCard?.def.label ?? ""} 历史曲线（近 2 年）`}
            </SectionTitle>

            {activeIsFlow && flowDays.length === 0 ? (
              <p className="py-10 text-center text-xs text-muted">
                交易所净流数据暂不可用（{flowApi.error?.message ?? "后端暂无该序列"}）
              </p>
            ) : !activeIsFlow && activeCard !== null && activeCard.series.length === 0 ? (
              <p className="py-10 text-center text-xs text-muted">该指标暂无历史数据（可能尚未采集）</p>
            ) : (
              <EChart option={chartOption} height={300} />
            )}
          </section>
        </>
      )}
    </div>
  );
}
