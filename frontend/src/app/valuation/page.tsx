"use client";

/**
 * 估值页：
 * - 估值五档仪表（分段条 + 指针）
 * - 综合评分 + 各维度评分（MVRV / Realized Cap / 成本基础 / 趋势 / 回撤）
 * - MVRV 历史曲线（/onchain/metrics/mvrv 历史序列）
 * - 免责声明：估值状态不代表未来涨跌
 */

import { useMemo } from "react";

import { ErrorState } from "@/components/common/ErrorState";
import { EvidenceList } from "@/components/common/EvidenceList";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";
import { EChart, PageHeader, SegmentBar, SectionTitle, StatTile } from "@/components/market/kit";
import { useApi } from "@/hooks/useApi";
import { useHydrated } from "@/hooks/useHydrated";
import {
  baseTooltip,
  CHART,
  daysAgoISO,
  extractSeriesRecords,
  labelDimension,
  scoreToPct,
  semanticTone,
  stateLabel,
  VALUATION_LEVELS,
  valuationLevelIndex,
} from "@/components/market/utils";
import { apiClient } from "@/lib/api";
import { useUIStore } from "@/stores/ui";
import type { EngineOutput, EngineOutputResponse, OnchainMetricPoint } from "@/types/api";
import type { EChartsOption } from "echarts";

/** 维度键 -> 展示名（后端键名不固定时的常规映射） */
const VALUATION_DIM_KEYS: Record<string, string> = {
  mvrv: "MVRV",
  realized_cap: "Realized Cap",
  cost_basis: "成本基础",
  trend: "趋势",
  drawdown: "回撤",
  nupl: "NUPL",
  rhodl: "RHODL",
  reserve_risk: "Reserve Risk",
  puell: "Puell",
};

function dimLabel(key: string): string {
  return VALUATION_DIM_KEYS[key.toLowerCase()] ?? labelDimension(key);
}

export default function ValuationPage() {
  const hydrated = useHydrated();
  const mode = useUIStore((s) => s.mode);
  const isPro = hydrated && mode === "pro"; // mode 必须无条件调用（rules-of-hooks）

  const valApi = useApi<EngineOutputResponse>(() => apiClient.engine.getValuation(), [], {
    refreshInterval: 120_000,
  });
  const mvrvApi = useApi<OnchainMetricPoint[]>(
    () =>
      apiClient.onchain.getMetricHistory("mvrv", {
        start: daysAgoISO(1460),
        end: daysAgoISO(0),
      }),
    [],
    { refreshInterval: 600_000 },
  );

  const out: EngineOutput | null = valApi.data?.output ?? null;
  const levelIndex = valuationLevelIndex(out?.state);
  const tone = out ? semanticTone(out.state) : "muted";

  const mvrvSeries = useMemo(
    () => extractSeriesRecords(mvrvApi.data),
    [mvrvApi.data],
  );

  const mvrvOption: EChartsOption = useMemo(() => {
    const labels = mvrvSeries.map((p) => (p.time ? p.time.slice(0, 10) : ""));
    const values = mvrvSeries.map((p) => p.value);
    return {
      tooltip: {
        trigger: "axis",
        ...baseTooltip(),
        valueFormatter: (v) => (typeof v === "number" ? v.toFixed(3) : "—"),
      },
      grid: { left: 8, right: 12, top: 24, bottom: 0, containLabel: true },
      xAxis: {
        type: "category",
        data: labels,
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
          data: values,
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
                { offset: 0, color: "rgba(88,166,255,0.28)" },
                { offset: 1, color: "rgba(88,166,255,0.02)" },
              ],
            },
          },
          markLine: {
            silent: true,
            symbol: "none",
            lineStyle: { color: CHART.warn, type: "dashed", width: 1 },
            label: { color: CHART.text, fontSize: 10 },
            data: [{ yAxis: 1, name: "持仓成本线" }],
          },
        },
      ],
    };
  }, [mvrvSeries]);

  const score = out?.score !== undefined && out.score !== null ? scoreToPct(out.score) : null;
  const dimEntries = Object.entries(out?.dimension_scores ?? {});

  const toneHex: Record<string, string> = {
    up: CHART.up,
    down: CHART.down,
    warn: CHART.warn,
    accent: CHART.accent,
    muted: CHART.muted,
  };

  return (
    <div>
      <PageHeader
        title="估值"
        subtitle="现在 BTC 贵不贵 · 基于 MVRV、Realized Cap、成本基础等链上估值指标的合成判断"
      />

      {valApi.error && !out && (
        <ErrorState
          error={valApi.error}
          lastUpdatedAt={valApi.lastUpdatedAt}
          onRetry={valApi.refresh}
          className="mb-4"
        />
      )}

      {valApi.loading && !out ? (
        <LoadingSkeleton variant="card" />
      ) : !out ? (
        <ErrorState error={valApi.error ?? undefined} lastUpdatedAt={valApi.lastUpdatedAt} onRetry={valApi.refresh}>
          <p className="text-xs text-muted">
            估值引擎暂无输出（可能尚未完成首轮计算）。数据落库后此页会自动展示。
          </p>
        </ErrorState>
      ) : (
        <div className="space-y-5">
          {/* 五档仪表 */}
          <section className="rounded-xl border border-line bg-bg-raised p-5 lg:p-8">
            <div className="mx-auto max-w-2xl">
              <div className="mb-5 text-center">
                <p className="font-mono text-[10px] uppercase tracking-[0.24em] text-muted">
                  市场估值状态
                </p>
                <p
                  className="mt-1.5 text-3xl font-bold tracking-tight"
                  style={{ color: toneHex[tone] }}
                >
                  {stateLabel(out.state)}
                </p>
                <p className="mt-1 font-mono text-xs tabular-nums text-muted">
                  置信度 {Math.round(out.confidence * 100)}%
                  {out.observation_time && hydrated
                    ? ` · ${out.observation_time.slice(0, 10)} 观测`
                    : ""}
                </p>
              </div>

              <SegmentBar
                levels={VALUATION_LEVELS}
                activeIndex={levelIndex}
                caption="估值状态描述当前价格相对历史持仓成本的位置，不代表未来涨跌方向。"
              />
            </div>
          </section>

          {/* 评分 */}
          <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <StatTile
              label="综合估值分"
              value={score !== null ? `${score}` : "—"}
              tone={tone}
              sub="0-100，越高越贵"
            />
            {dimEntries.slice(0, 3).map(([key, value]) => (
              <StatTile
                key={key}
                label={dimLabel(key)}
                value={scoreToPct(value)}
                tone="accent"
                sub={`归一化得分 · ${isPro ? `原始 ${typeof value === "number" ? value.toFixed(4) : String(value)}` : "0-100"}`}
              />
            ))}
            {dimEntries.length === 0 && (
              <div className="rounded-xl border border-dashed border-line p-4 text-xs text-muted sm:col-span-2 lg:col-span-3">
                引擎未返回分维度评分（专业模式下查看原始权重明细）。
              </div>
            )}
          </section>

          {/* MVRV 曲线 */}
          <section className="rounded-xl border border-line bg-bg-raised p-4">
            <SectionTitle
              right={
                <span className="text-[10px] text-muted">
                  虚线为 1.0（市场整体持仓成本线）· 数据暂无法更新时展示缓存曲线
                </span>
              }
            >
              MVRV 历史曲线（近 4 年）
            </SectionTitle>

            {mvrvApi.loading && mvrvSeries.length === 0 ? (
              <LoadingSkeleton variant="lines" rows={4} className="py-16" />
            ) : mvrvSeries.length > 0 ? (
              <EChart option={mvrvOption} height={320} />
            ) : (
              <p className="py-10 text-center text-xs text-muted">
                MVRV 历史数据暂不可用（{mvrvApi.error?.message ?? "后端暂无该序列"}）
              </p>
            )}
          </section>

          {/* 证据 */}
          <section className="grid gap-4 md:grid-cols-2">
            <div className="rounded-xl border border-line bg-bg-raised p-4">
              <EvidenceList
                evidence={{ supporting: out.supporting_evidence, opposing: [] }}
                emptyText="暂无支持证据"
              />
            </div>
            <div className="rounded-xl border border-line bg-bg-raised p-4">
              <EvidenceList
                evidence={{ supporting: [], opposing: out.opposing_evidence }}
                emptyText="暂无反对证据"
              />
            </div>
          </section>

          {isPro && (
            <section className="rounded-xl border border-line bg-bg-raised p-4 text-[11px]">
              <SectionTitle>模型信息（专业模式）</SectionTitle>
              <dl className="grid gap-x-8 gap-y-2 sm:grid-cols-2">
                {out.model_version && (
                  <div className="flex justify-between gap-4">
                    <dt className="text-muted">模型版本</dt>
                    <dd className="font-mono text-[#e6edf3]">{out.model_version}</dd>
                  </div>
                )}
                <div className="flex justify-between gap-4">
                  <dt className="text-muted">原始状态码</dt>
                  <dd className="font-mono text-[#e6edf3]">{out.state}</dd>
                </div>
                {out.data_gaps && out.data_gaps.length > 0 && (
                  <div className="flex justify-between gap-4 sm:col-span-2">
                    <dt className="shrink-0 text-muted">数据缺口</dt>
                    <dd className="text-right font-mono text-warn">{out.data_gaps.join("、")}</dd>
                  </div>
                )}
              </dl>
              <p className="mt-3 text-muted">
                MVRV = 市值 / 已实现市值：大于 1 表示整体持仓处于浮盈状态，历史顶部区域通常伴随显著高估。
              </p>
            </section>
          )}
        </div>
      )}
    </div>
  );
}
