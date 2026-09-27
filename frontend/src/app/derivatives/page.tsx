"use client";

/**
 * 衍生品页：
 * - Funding Rate（当前值 + 多交易所明细 + 近 90 天曲线）
 * - Open Interest（当前值 + 曲线）
 * - 24h 多空清算（占比条）
 * - 多空比（占比条）
 */

import { useMemo } from "react";

import { DataSourceBadge } from "@/components/common/DataSourceBadge";
import { ErrorState } from "@/components/common/ErrorState";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";
import { QualityBadge } from "@/components/common/QualityBadge";
import { EChart, PageHeader, SectionTitle, SplitBar, StatTile } from "@/components/market/kit";
import { useApi } from "@/hooks/useApi";
import {
  baseTooltip,
  CHART,
  daysAgoISO,
  extractSeriesRecords,
  formatRatioPct,
  formatUsd,
  pickNumber,
  pickString,
  type SeriesPoint,
  shortDate,
} from "@/components/market/utils";
import { apiClient } from "@/lib/api";
import type { SeriesQuery } from "@/lib/api";
import type { FundingData, LiquidationData, OpenInterest } from "@/types/api";
import type { EChartsOption } from "echarts";

/** OI 历史序列（宽松行结构：oi_usd / oi / total_oi） */
function oiSeries(raw: unknown): SeriesPoint[] {
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
  const out: SeriesPoint[] = [];
  for (const row of rows) {
    if (!row || typeof row !== "object") continue;
    const time = shortDate(pickString(row, ["date", "time", "observation_time", "day"]));
    const value = pickNumber(row, ["oi_usd", "total_oi_usd", "open_interest_usd", "oi", "total_oi"]);
    if (time && value !== null) out.push({ time, value });
  }
  return out;
}

/** 多空比 -> 多头占比 %（兼容 pct 键与 ratio 键两种语义） */
function longPctOf(raw: unknown): number | null {
  const pct = pickNumber(raw, ["long_account_pct", "long_pct", "long_percent", "global_long_pct"]);
  if (pct !== null) return pct <= 1 ? pct * 100 : pct;
  const ratio = pickNumber(raw, ["long_ratio", "long_short_ratio"]);
  if (ratio !== null) return ratio <= 1 ? ratio * 100 : (ratio / (1 + ratio)) * 100;
  if (raw && typeof raw === "object") {
    const o = raw as Record<string, unknown>;
    for (const k of ["current", "latest", "data", "result", "long_short"]) {
      const v = o[k];
      if (v && typeof v === "object") {
        const nested = longPctOf(v);
        if (nested !== null) return nested;
      }
    }
  }
  return null;
}

function toneOf(v: number | null): "up" | "down" | "default" {
  if (v === null) return "default";
  return v >= 0 ? "up" : "down";
}

export default function DerivativesPage() {
  const fundingApi = useApi<FundingData>(() => apiClient.derivatives.getFunding({ limit: 90 }), [], {
    refreshInterval: 60_000,
  });
  const oiApi = useApi<OpenInterest>(
    () => apiClient.derivatives.getOpenInterest({ start: daysAgoISO(90), end: daysAgoISO(0) }),
    [],
    { refreshInterval: 120_000 },
  );
  const liqApi = useApi<LiquidationData>(
    // period 不在 SeriesQuery 类型内，但后端 /derivatives/liquidations 支持
    () => apiClient.derivatives.getLiquidations({ period: "24h" } as unknown as SeriesQuery),
    [],
    { refreshInterval: 60_000 },
  );
  const lsApi = useApi<Record<string, unknown>>(() => apiClient.derivatives.getLongShort({ limit: 30 }), [], {
    refreshInterval: 120_000,
  });

  /* ---------------- Funding ---------------- */
  const funding = fundingApi.data;
  const fundingRate = pickNumber(funding, ["funding_rate", "predicted_rate", "rate"]);
  const fundingByExchange = Array.isArray(funding?.by_exchange) ? funding!.by_exchange : [];
  const fundingSeries = useMemo(
    () => extractSeriesRecords(funding?.history ?? funding),
    [funding],
  );

  const fundingOption: EChartsOption = useMemo(() => {
    if (fundingSeries.length === 0) return {};
    return {
      tooltip: {
        trigger: "axis",
        ...baseTooltip(),
        valueFormatter: (v) => (typeof v === "number" ? formatRatioPct(v) : "—"),
      },
      grid: { left: 8, right: 12, top: 20, bottom: 0, containLabel: true },
      xAxis: {
        type: "category",
        data: fundingSeries.map((p) => (p.time ? p.time.slice(0, 10) : "")),
        boundaryGap: false,
        axisLine: { lineStyle: { color: CHART.axis } },
        axisTick: { show: false },
        axisLabel: { color: CHART.text, fontSize: 10 },
      },
      yAxis: {
        type: "value",
        splitLine: { lineStyle: { color: CHART.grid } },
        axisLabel: { color: CHART.text, fontSize: 10, formatter: (v: number) => formatRatioPct(v, 2) },
      },
      series: [
        {
          type: "line",
          data: fundingSeries.map((p) => p.value),
          showSymbol: false,
          lineStyle: { color: CHART.warn, width: 1.6 },
          markLine: {
            silent: true,
            symbol: "none",
            lineStyle: { color: CHART.muted, type: "dashed", width: 1 },
            label: { show: false },
            data: [{ yAxis: 0 }],
          },
        },
      ],
    };
  }, [fundingSeries]);

  /* ---------------- Open Interest ---------------- */
  const oi = oiApi.data;
  const oiCurrent = pickNumber(oi, ["total_oi_usd", "oi_usd", "total_oi", "open_interest"]);
  const oiCurve = useMemo(() => oiSeries(oi), [oi]);

  const oiOption: EChartsOption = useMemo(() => {
    if (oiCurve.length === 0) return {};
    return {
      tooltip: {
        trigger: "axis",
        ...baseTooltip(),
        valueFormatter: (v) => formatUsd(typeof v === "number" ? v : null),
      },
      grid: { left: 8, right: 12, top: 20, bottom: 0, containLabel: true },
      xAxis: {
        type: "category",
        data: oiCurve.map((p) => p.time),
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
          data: oiCurve.map((p) => p.value),
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
                { offset: 0, color: "rgba(88,166,255,0.22)" },
                { offset: 1, color: "rgba(88,166,255,0.02)" },
              ],
            },
          },
        },
      ],
    };
  }, [oiCurve]);

  /* ---------------- Liquidations（24h） ---------------- */
  const liq = liqApi.data;
  const longLiq = pickNumber(liq, ["long_liq_24h", "long_liquidations_usd", "long_liq_usd"]);
  const shortLiq = pickNumber(liq, ["short_liq_24h", "short_liquidations_usd", "short_liq_usd"]);
  const totalLiq =
    pickNumber(liq, ["total_liq_24h", "total_liq_usd"]) ??
    (longLiq !== null || shortLiq !== null ? (longLiq ?? 0) + (shortLiq ?? 0) : null);
  const liqLongPct =
    longLiq !== null || shortLiq !== null
      ? ((longLiq ?? 0) / Math.max((longLiq ?? 0) + (shortLiq ?? 0), 1e-9)) * 100
      : null;

  /* ---------------- 多空比 ---------------- */
  const ls = lsApi.data;
  const lsLongPct = useMemo(() => longPctOf(ls), [ls]);

  const anyLoading =
    fundingApi.loading && oiApi.loading && liqApi.loading && lsApi.loading;
  const hasAnyData = funding !== null || oi !== null || liq !== null || ls !== null;

  return (
    <div>
      <PageHeader
        title="衍生品"
        subtitle="永续合约资金费率、未平仓合约、清算与多空情绪 · 杠杆热度是风险引擎的关键输入"
        right={
          <>
            <QualityBadge
              status={fundingApi.meta?.quality_status ?? oiApi.meta?.quality_status ?? null}
            />
            <DataSourceBadge source={fundingApi.meta?.source} updatedAt={fundingApi.meta?.timestamp} />
          </>
        }
      />

      {fundingApi.error && !hasAnyData && (
        <ErrorState
          error={fundingApi.error}
          lastUpdatedAt={fundingApi.lastUpdatedAt}
          onRetry={() => {
            fundingApi.refresh();
            oiApi.refresh();
            liqApi.refresh();
            lsApi.refresh();
          }}
          className="mb-4"
        />
      )}

      {anyLoading && !hasAnyData ? (
        <LoadingSkeleton variant="card" />
      ) : (
        <div className="grid gap-4 lg:grid-cols-3">
          {/* Funding Rate */}
          <section className="rounded-xl border border-line bg-bg-raised p-4 lg:col-span-2">
            <SectionTitle
              right={
                <span className="text-[10px] text-muted">
                  正=多头付费（多头拥挤），负=空头付费 · 每 8 小时结算
                </span>
              }
            >
              资金费率 Funding Rate
            </SectionTitle>

            <div className="flex flex-wrap items-end gap-6">
              <StatTile
                label="当前费率"
                value={<span>{formatRatioPct(fundingRate)}</span>}
                tone={toneOf(fundingRate)}
                className="min-w-[180px] !border-0 !bg-transparent !p-0"
              />
              {funding?.next_funding_time && (
                <div className="pb-1 text-[11px] text-muted">
                  下次结算 <span className="font-mono tabular-nums">{funding.next_funding_time.slice(0, 16).replace("T", " ")}</span>
                </div>
              )}
            </div>

            {fundingSeries.length > 0 ? (
              <div className="mt-3">
                <EChart option={fundingOption} height={180} />
              </div>
            ) : (
              <p className="mt-3 text-xs text-muted">费率历史暂不可用（{fundingApi.error?.message ?? "暂无序列"}）</p>
            )}

            {fundingByExchange.length > 0 && (
              <div className="mt-3 grid gap-1.5 sm:grid-cols-2">
                {fundingByExchange.slice(0, 8).map((item, i) => {
                  const ex = pickString(item, ["exchange", "name", "venue"]) ?? `交易所 #${i + 1}`;
                  const rate = pickNumber(item, ["funding_rate", "rate", "value"]);
                  return (
                    <div
                      key={`${ex}-${i}`}
                      className="flex items-center justify-between rounded-lg border border-line bg-bg px-2.5 py-1.5 text-xs"
                    >
                      <span className="text-muted">{ex}</span>
                      <span
                        className={`font-mono tabular-nums ${
                          rate === null ? "text-muted" : rate >= 0 ? "text-up" : "text-down"
                        }`}
                      >
                        {rate !== null ? formatRatioPct(rate) : "—"}
                      </span>
                    </div>
                  );
                })}
              </div>
            )}
          </section>

          {/* 右列：清算 + 多空比 */}
          <div className="space-y-4">
            <section className="rounded-xl border border-line bg-bg-raised p-4">
              <SectionTitle right={<span className="text-[10px] text-muted">金额为美元估计</span>}>
                24h 清算
              </SectionTitle>
              <p className="font-mono text-lg font-semibold tabular-nums text-[#e6edf3]">
                {totalLiq !== null ? formatUsd(totalLiq) : "—"}
              </p>
              {liqLongPct !== null ? (
                <div className="mt-3">
                  <SplitBar
                    leftLabel={`多头清算 ${formatUsd(longLiq)}`}
                    rightLabel={`空头清算 ${formatUsd(shortLiq)}`}
                    leftPct={liqLongPct}
                    leftColor={CHART.down}
                    rightColor={CHART.up}
                  />
                  <p className="mt-2 text-[11px] leading-relaxed text-muted">
                    {liqLongPct >= 70
                      ? "多头被大规模清算（行情下跌触发踩踏）"
                      : liqLongPct <= 30
                        ? "空头被大规模清算（行情上涨轧空）"
                        : "多空清算相对均衡"}
                  </p>
                </div>
              ) : (
                <p className="mt-2 text-xs text-muted">
                  清算数据暂不可用（{liqApi.error?.message ?? "暂无数据"}）
                </p>
              )}
            </section>

            <section className="rounded-xl border border-line bg-bg-raised p-4">
              <SectionTitle>多空比</SectionTitle>
              {lsLongPct !== null ? (
                <>
                  <SplitBar
                    leftLabel={`多头 ${lsLongPct.toFixed(1)}%`}
                    rightLabel={`空头 ${(100 - lsLongPct).toFixed(1)}%`}
                    leftPct={lsLongPct}
                  />
                  <p className="mt-2 text-[11px] leading-relaxed text-muted">
                    账户数/持仓的多空分布。多头占比过高时，行情反转更易触发连环清算。
                  </p>
                </>
              ) : (
                <p className="text-xs text-muted">
                  多空比数据暂不可用（{lsApi.error?.message ?? "暂无数据"}）
                </p>
              )}
            </section>
          </div>

          {/* Open Interest */}
          <section className="rounded-xl border border-line bg-bg-raised p-4 lg:col-span-3">
            <SectionTitle
              right={
                <span className="text-[10px] text-muted">
                  未平仓合约名义价值 · 上升=新资金入场，骤降=杠杆出清
                </span>
              }
            >
              未平仓合约 Open Interest
            </SectionTitle>
            <p className="font-mono text-lg font-semibold tabular-nums text-[#e6edf3]">
              {oiCurrent !== null ? formatUsd(oiCurrent) : "—"}
            </p>

            {oiCurve.length > 0 ? (
              <div className="mt-3">
                <EChart option={oiOption} height={220} />
              </div>
            ) : (
              <p className="mt-2 text-xs text-muted">
                OI 历史暂不可用（{oiApi.error?.message ?? "暂无序列"}）
              </p>
            )}

            {Array.isArray(oi?.by_exchange) && oi!.by_exchange.length > 0 && (
              <div className="mt-3 grid gap-1.5 sm:grid-cols-3 lg:grid-cols-4">
                {oi!.by_exchange.slice(0, 8).map((item, i) => {
                  const ex = pickString(item, ["exchange", "name", "venue"]) ?? `交易所 #${i + 1}`;
                  const val = pickNumber(item, ["oi_usd", "oi", "total_oi"]);
                  return (
                    <div
                      key={`${ex}-${i}`}
                      className="flex items-center justify-between rounded-lg border border-line bg-bg px-2.5 py-1.5 text-xs"
                    >
                      <span className="text-muted">{ex}</span>
                      <span className="font-mono tabular-nums text-[#e6edf3]">
                        {val !== null ? formatUsd(val) : "—"}
                      </span>
                    </div>
                  );
                })}
              </div>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
