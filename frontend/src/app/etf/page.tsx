"use client";

/**
 * ETF 页（现货 BTC ETF）：
 * - 每日净流入柱状图（正绿负红，ECharts）
 * - 1D / 7D / 30D / 90D 汇总卡 + ETF 总持仓
 * - 累积净流入曲线
 */

import { useMemo } from "react";

import { ErrorState } from "@/components/common/ErrorState";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";
import { QualityBadge } from "@/components/common/QualityBadge";
import { EChart, PageHeader, SectionTitle, StatTile } from "@/components/market/kit";
import { useApi } from "@/hooks/useApi";
import {
  baseTooltip,
  CHART,
  daysAgoISO,
  formatNumber,
  formatUsd,
  pickNumber,
  pickString,
  shortDate,
} from "@/components/market/utils";
import { apiClient } from "@/lib/api";
import type { ETFSummary } from "@/types/api";
import type { EChartsOption } from "echarts";

interface FlowDay {
  date: string;
  net: number;
}

/**
 * 后端 /etf/flows 可能返回行数组或 { daily_flows: [...] } 包裹；
 * 多 ticker 行按日聚合求和。
 */
function parseDailyFlows(raw: unknown): FlowDay[] {
  let rows: unknown[] = [];
  if (Array.isArray(raw)) rows = raw;
  else if (raw && typeof raw === "object") {
    const o = raw as Record<string, unknown>;
    for (const k of ["daily_flows", "flows", "history", "rows", "items", "data"]) {
      if (Array.isArray(o[k])) {
        rows = o[k] as unknown[];
        break;
      }
    }
  }
  const byDate = new Map<string, number>();
  for (const row of rows) {
    if (!row || typeof row !== "object") continue;
    const date = shortDate(pickString(row, ["date", "trade_date", "time", "day"]));
    if (!date) continue;
    const net = pickNumber(row, ["net_flow", "net_flow_usd", "total_net_flow", "netflow"]);
    if (net === null) continue;
    byDate.set(date, (byDate.get(date) ?? 0) + net);
  }
  return [...byDate.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([date, net]) => ({ date, net }));
}

/** ETF 总持仓（BTC）：兼容数字 / 按 ticker 行数组 / 嵌套对象 */
function parseHoldingsBtc(raw: unknown): number | null {
  if (typeof raw === "number" && Number.isFinite(raw)) return raw;
  if (Array.isArray(raw)) {
    let sum = 0;
    let has = false;
    for (const item of raw) {
      const n = pickNumber(item, ["total_holdings_btc", "holdings_btc", "btc", "balance", "amount"]);
      if (n !== null) {
        sum += n;
        has = true;
      }
    }
    return has ? sum : null;
  }
  if (raw && typeof raw === "object") {
    const direct = pickNumber(raw, ["total_holdings_btc", "total_btc", "holdings_btc", "total"]);
    if (direct !== null) return direct;
    let sum = 0;
    let has = false;
    for (const v of Object.values(raw as Record<string, unknown>)) {
      if (v && typeof v === "object") {
        const n = pickNumber(v, ["total_holdings_btc", "holdings_btc", "btc", "balance"]);
        if (n !== null) {
          sum += n;
          has = true;
        }
      }
    }
    return has ? sum : null;
  }
  return null;
}

function sumLastN(flows: FlowDay[], n: number): number | null {
  if (flows.length === 0) return null;
  return flows.slice(-n).reduce((s, f) => s + f.net, 0);
}

function toneOf(v: number | null): "up" | "down" | "default" {
  if (v === null) return "default";
  return v >= 0 ? "up" : "down";
}

export default function EtfPage() {
  const flowsApi = useApi<unknown>(
    () => apiClient.etf.getFlows({ start: daysAgoISO(90), end: daysAgoISO(0) }),
    [],
    { refreshInterval: 300_000 },
  );
  const summaryApi = useApi<ETFSummary>(() => apiClient.etf.getSummary(), [], {
    refreshInterval: 300_000,
  });

  const flows = useMemo(() => parseDailyFlows(flowsApi.data), [flowsApi.data]);

  const summary = summaryApi.data;
  const holdingsBtc = useMemo(() => {
    if (!summary) return null;
    const direct = summary.total_holdings_btc;
    if (typeof direct === "number" && Number.isFinite(direct)) return direct;
    return parseHoldingsBtc(summary["holdings"] ?? summary);
  }, [summary]);

  const flow1d =
    flows.length > 0
      ? flows[flows.length - 1].net
      : summary?.latest_net_flow != null
        ? summary.latest_net_flow
        : null;
  const flow7d = sumLastN(flows, 7) ?? (summary?.net_flow_7d ?? null);
  const flow30d = sumLastN(flows, 30) ?? (summary?.net_flow_30d ?? null);
  const flow90d = sumLastN(flows, 90) ?? (summary?.net_flow_90d ?? null);

  /** 累积净流入曲线（for-of 同步循环，避免闭包重赋值） */
  const cumulative = useMemo(() => {
    const out: { date: string; value: number }[] = [];
    let acc = 0;
    for (const f of flows) {
      acc += f.net;
      out.push({ date: f.date, value: acc });
    }
    return out;
  }, [flows]);

  const dailyOption: EChartsOption = useMemo(
    () => ({
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        ...baseTooltip(),
        valueFormatter: (v) => formatUsd(typeof v === "number" ? v : null),
      },
      grid: { left: 8, right: 8, top: 24, bottom: 0, containLabel: true },
      xAxis: {
        type: "category",
        data: flows.map((f) => f.date),
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
          data: flows.map((f) => ({
            value: f.net,
            itemStyle: { color: f.net >= 0 ? "rgba(63,185,80,0.8)" : "rgba(248,81,73,0.8)" },
          })),
          barMaxWidth: 12,
        },
      ],
    }),
    [flows],
  );

  const cumulativeOption: EChartsOption = useMemo(
    () => ({
      tooltip: {
        trigger: "axis",
        ...baseTooltip(),
        valueFormatter: (v) => formatUsd(typeof v === "number" ? v : null),
      },
      grid: { left: 8, right: 12, top: 24, bottom: 0, containLabel: true },
      xAxis: {
        type: "category",
        boundaryGap: false,
        data: cumulative.map((p) => p.date),
        axisLine: { lineStyle: { color: CHART.axis } },
        axisTick: { show: false },
        axisLabel: { color: CHART.text, fontSize: 10 },
      },
      yAxis: {
        type: "value",
        scale: true,
        splitLine: { lineStyle: { color: CHART.grid } },
        axisLabel: { color: CHART.text, fontSize: 10, formatter: (v: number) => formatUsd(v, 0) },
      },
      series: [
        {
          type: "line",
          data: cumulative.map((p) => p.value),
          showSymbol: false,
          lineStyle: { color: CHART.accent, width: 1.8 },
          areaStyle: {
            color: {
              type: "linear",
              x: 0,
              y: 0,
              x2: 0,
              y2: 1,
              colorStops: [
                { offset: 0, color: "rgba(88,166,255,0.25)" },
                { offset: 1, color: "rgba(88,166,255,0.02)" },
              ],
            },
          },
        },
      ],
    }),
    [cumulative],
  );

  const hasAnyData = flows.length > 0 || summary !== null;
  const noFlows = flows.length === 0;

  return (
    <div>
      <PageHeader
        title="ETF 资金流"
        subtitle="美国现货 BTC ETF 每日净申购 · 正值=净流入（买入 BTC），负值=净流出"
        right={
          <QualityBadge
            status={flowsApi.meta?.quality_status ?? summaryApi.meta?.quality_status ?? null}
          />
        }
      />

      {flowsApi.error && !hasAnyData && (
        <ErrorState
          error={flowsApi.error ?? summaryApi.error}
          lastUpdatedAt={flowsApi.lastUpdatedAt ?? summaryApi.lastUpdatedAt}
          onRetry={() => {
            flowsApi.refresh();
            summaryApi.refresh();
          }}
          className="mb-4"
        />
      )}

      {flowsApi.loading && !hasAnyData ? (
        <LoadingSkeleton variant="card" />
      ) : (
        <div className="space-y-5">
          {/* 汇总卡 */}
          <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            <StatTile
              label="1D 净流"
              value={flow1d !== null ? formatUsd(flow1d) : "—"}
              tone={toneOf(flow1d)}
            />
            <StatTile
              label="7D 净流"
              value={flow7d !== null ? formatUsd(flow7d) : "—"}
              tone={toneOf(flow7d)}
            />
            <StatTile
              label="30D 净流"
              value={flow30d !== null ? formatUsd(flow30d) : "—"}
              tone={toneOf(flow30d)}
            />
            <StatTile
              label="90D 净流"
              value={flow90d !== null ? formatUsd(flow90d) : "—"}
              tone={toneOf(flow90d)}
            />
            <StatTile
              label="ETF 总持仓"
              value={holdingsBtc !== null ? `${formatNumber(holdingsBtc, 0)} BTC` : "—"}
              sub={
                summary?.holdings_pct_of_supply != null
                  ? `约占总量 ${summary.holdings_pct_of_supply}%`
                  : undefined
              }
            />
          </section>

          {/* 每日净流柱状图 */}
          <section className="rounded-xl border border-line bg-bg-raised p-4">
            <SectionTitle
              right={
                <span className="text-[10px] text-muted">
                  {noFlows ? "净流序列暂不可用，显示后端汇总值" : "近 90 个交易日"}
                </span>
              }
            >
              每日净流入（USD）
            </SectionTitle>
            {noFlows ? (
              <p className="py-10 text-center text-xs text-muted">
                每日净流数据暂不可用（{flowsApi.error?.message ?? "后端暂无该序列"}）
              </p>
            ) : (
              <EChart option={dailyOption} height={300} />
            )}
          </section>

          {/* 累积流量曲线 */}
          {!noFlows && (
            <section className="rounded-xl border border-line bg-bg-raised p-4">
              <SectionTitle right={<span className="text-[10px] text-muted">窗口内累积</span>}>
                累积净流入（USD）
              </SectionTitle>
              <EChart option={cumulativeOption} height={260} />
            </section>
          )}
        </div>
      )}
    </div>
  );
}
