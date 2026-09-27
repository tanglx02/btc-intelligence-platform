"use client";

/**
 * 资金流页（交易所）：
 * - 交易所每日净流入/流出柱状图（正绿负红）
 * - 流入 vs 流出双线
 * - 交易所 BTC 储备变化曲线
 * - 稳定币流量（数据源未接入时降级说明）
 */

import { useMemo } from "react";

import { EmptyState } from "@/components/common/EmptyState";
import { ErrorState } from "@/components/common/ErrorState";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";
import { QualityBadge } from "@/components/common/QualityBadge";
import { EChart, PageHeader, SectionTitle, StatTile } from "@/components/market/kit";
import { useApi } from "@/hooks/useApi";
import {
  baseTooltip,
  CHART,
  daysAgoISO,
  formatUsd,
  pickNumber,
  pickString,
  shortDate,
} from "@/components/market/utils";
import { apiClient } from "@/lib/api";
import type { EChartsOption } from "echarts";

const RANGE_DAYS = 90;

interface FlowDay {
  date: string;
  inflow: number;
  outflow: number;
  net: number;
  balance: number | null;
}

/**
 * 交易所资金流行数据（后端为行数组：inflow_usd / outflow_usd / net_flow_usd /
 * exchange_balance ...），兼容聚合包裹结构；按日聚合，同日多行 balance 取最后非空。
 */
function parseExchangeFlow(raw: unknown): FlowDay[] {
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
    if (rows.length === 0) {
      // 单点聚合形态 { inflow, outflow, netflow }
      const hasAny =
        pickNumber(o, ["inflow", "inflow_usd"]) !== null ||
        pickNumber(o, ["netflow", "net_flow", "net_flow_usd"]) !== null;
      if (hasAny) rows = [o];
    }
  }

  interface Acc {
    inflow: number;
    outflow: number;
    net: number;
    balance: number | null;
  }
  const byDate = new Map<string, Acc>();
  for (const row of rows) {
    if (!row || typeof row !== "object") continue;
    const date = shortDate(pickString(row, ["observation_time", "date", "time", "day"]));
    if (!date) continue;
    const inflow = pickNumber(row, ["inflow_usd", "inflow"]);
    const outflow = pickNumber(row, ["outflow_usd", "outflow"]);
    let net = pickNumber(row, ["net_flow_usd", "netflow_usd", "net_flow", "netflow"]);
    if (net === null && inflow !== null && outflow !== null) net = inflow - outflow;
    if (inflow === null && outflow === null && net === null) continue;
    const balance = pickNumber(row, ["exchange_balance", "balance", "total_balance"]);
    const acc = byDate.get(date) ?? { inflow: 0, outflow: 0, net: 0, balance: null };
    acc.inflow += inflow ?? 0;
    acc.outflow += outflow ?? 0;
    acc.net += net ?? 0;
    if (balance !== null) acc.balance = balance;
    byDate.set(date, acc);
  }
  return [...byDate.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([date, acc]) => ({ date, ...acc }));
}

function sumLastN(days: FlowDay[], n: number, key: "net" | "inflow" | "outflow"): number | null {
  if (days.length === 0) return null;
  return days.slice(-n).reduce((s, d) => s + d[key], 0);
}

function toneOf(v: number | null): "up" | "down" | "default" {
  if (v === null) return "default";
  return v >= 0 ? "up" : "down";
}

export default function ExchangeFlowPage() {
  const flowApi = useApi<unknown>(
    () => apiClient.onchain.getExchangeFlow({ start: daysAgoISO(RANGE_DAYS), end: daysAgoISO(0) }),
    [],
    { refreshInterval: 300_000 },
  );

  const days = useMemo(() => parseExchangeFlow(flowApi.data), [flowApi.data]);

  const latest = days.length > 0 ? days[days.length - 1] : null;
  const net7d = sumLastN(days, 7, "net");
  const net30d = sumLastN(days, 30, "net");
  const balanceNow = useMemo(() => {
    for (let i = days.length - 1; i >= 0; i -= 1) {
      if (days[i].balance !== null) return days[i].balance;
    }
    return null;
  }, [days]);
  const balancePrev = useMemo(() => {
    if (days.length < 8) return null;
    for (let i = Math.max(days.length - 31, 0); i >= 0; i -= 1) {
      if (days[i].balance !== null) return days[i].balance;
    }
    return null;
  }, [days]);
  const balanceDelta =
    balanceNow !== null && balancePrev !== null ? balanceNow - balancePrev : null;

  /** 每日净流柱状图 */
  const netOption: EChartsOption = useMemo(
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
        data: days.map((d) => d.date),
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
          data: days.map((d) => ({
            value: d.net,
            itemStyle: { color: d.net >= 0 ? "rgba(63,185,80,0.8)" : "rgba(248,81,73,0.8)" },
          })),
          barMaxWidth: 10,
        },
      ],
    }),
    [days],
  );

  /** 流入 vs 流出双线 */
  const ioOption: EChartsOption = useMemo(
    () => ({
      tooltip: {
        trigger: "axis",
        ...baseTooltip(),
        valueFormatter: (v) => formatUsd(typeof v === "number" ? v : null),
      },
      legend: {
        data: ["流入", "流出"],
        textStyle: { color: CHART.text, fontSize: 10 },
        top: 0,
      },
      grid: { left: 8, right: 12, top: 30, bottom: 0, containLabel: true },
      xAxis: {
        type: "category",
        data: days.map((d) => d.date),
        boundaryGap: false,
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
          name: "流入",
          data: days.map((d) => d.inflow),
          showSymbol: false,
          lineStyle: { color: CHART.up, width: 1.5 },
          itemStyle: { color: CHART.up },
        },
        {
          type: "line",
          name: "流出",
          data: days.map((d) => d.outflow),
          showSymbol: false,
          lineStyle: { color: CHART.down, width: 1.5 },
          itemStyle: { color: CHART.down },
        },
      ],
    }),
    [days],
  );

  /** 储备曲线（balance 非空的点） */
  const balancePoints = useMemo(() => days.filter((d) => d.balance !== null), [days]);

  const balanceOption: EChartsOption = useMemo(() => {
    if (balancePoints.length === 0) return {};
    return {
      tooltip: {
        trigger: "axis",
        ...baseTooltip(),
        valueFormatter: (v) => formatUsd(typeof v === "number" ? v : null),
      },
      grid: { left: 8, right: 12, top: 24, bottom: 0, containLabel: true },
      xAxis: {
        type: "category",
        data: balancePoints.map((d) => d.date),
        boundaryGap: false,
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
          data: balancePoints.map((d) => d.balance),
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
                { offset: 0, color: "rgba(88,166,255,0.24)" },
                { offset: 1, color: "rgba(88,166,255,0.02)" },
              ],
            },
          },
        },
      ],
    };
  }, [balancePoints]);

  const hasData = days.length > 0;

  return (
    <div>
      <PageHeader
        title="交易所资金流"
        subtitle="链上充值/提币与交易所 BTC 储备 · 储备持续下降通常意味着长期提币自托管"
        right={<QualityBadge status={flowApi.meta?.quality_status ?? null} />}
      />

      {flowApi.error && !hasData && (
        <ErrorState
          error={flowApi.error}
          lastUpdatedAt={flowApi.lastUpdatedAt}
          onRetry={flowApi.refresh}
          className="mb-4"
        />
      )}

      {flowApi.loading && !hasData ? (
        <LoadingSkeleton variant="card" />
      ) : !hasData ? (
        <EmptyState
          title="暂无交易所资金流数据"
          description="后端尚未采集到资金流序列，数据落库后此页会自动展示。"
          className="mt-2"
        />
      ) : (
        <div className="space-y-5">
          {/* 汇总卡 */}
          <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <StatTile
              label="最近一日净流"
              value={latest ? formatUsd(latest.net) : "—"}
              tone={toneOf(latest?.net ?? null)}
              sub={latest ? latest.date : undefined}
            />
            <StatTile
              label="7D 净流"
              value={net7d !== null ? formatUsd(net7d) : "—"}
              tone={toneOf(net7d)}
            />
            <StatTile
              label="30D 净流"
              value={net30d !== null ? formatUsd(net30d) : "—"}
              tone={toneOf(net30d)}
            />
            <StatTile
              label="交易所储备"
              value={balanceNow !== null ? formatUsd(balanceNow) : "—"}
              sub={
                balanceDelta !== null
                  ? `30 日变化 ${formatUsd(balanceDelta)}`
                  : "全部链上交易所余额估值"
              }
              tone={toneOf(balanceDelta === null ? null : -balanceDelta)}
            />
          </section>

          {/* 每日净流 */}
          <section className="rounded-xl border border-line bg-bg-raised p-4">
            <SectionTitle
              right={
                <span className="text-[10px] text-muted">
                  正值=净流入（充值，潜在抛压），负值=净流出（提币）
                </span>
              }
            >
              每日净流入 / 流出（USD）
            </SectionTitle>
            <EChart option={netOption} height={300} />
          </section>

          {/* 流入 vs 流出 */}
          <section className="rounded-xl border border-line bg-bg-raised p-4">
            <SectionTitle>流入 vs 流出（USD / 日）</SectionTitle>
            <EChart option={ioOption} height={260} />
          </section>

          {/* 储备变化 */}
          {balancePoints.length > 1 ? (
            <section className="rounded-xl border border-line bg-bg-raised p-4">
              <SectionTitle right={<span className="text-[10px] text-muted">交易所钱包余额估值</span>}>
                交易所 BTC 储备变化
              </SectionTitle>
              <EChart option={balanceOption} height={260} />
            </section>
          ) : null}

          {/* 稳定币流量（数据源未接入时降级说明） */}
          <section>
            <SectionTitle>稳定币流量</SectionTitle>
            <EmptyState
              title="稳定币流量数据暂未接入"
              description="稳定币铸造/赎回与交易所稳定币流入数据源尚在接入中，上线后此板块将自动展示。"
            />
          </section>
        </div>
      )}
    </div>
  );
}
