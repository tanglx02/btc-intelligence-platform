"use client";

/**
 * 期权页：
 * - 期权概览（总 OI / 24h 成交量 / Max Pain）
 * - Put/Call Ratio（占比条 + 历史）
 * - IV 曲线（期限结构）+ DVOL / 偏度 / 分位
 */

import { useMemo } from "react";

import { ErrorState } from "@/components/common/ErrorState";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";
import { QualityBadge } from "@/components/common/QualityBadge";
import { EChart, PageHeader, SectionTitle, SplitBar, StatTile } from "@/components/market/kit";
import { useApi } from "@/hooks/useApi";
import {
  baseTooltip,
  CHART,
  formatNumber,
  formatPrice,
  formatUsd,
  pickNumber,
  pickString,
} from "@/components/market/utils";
import { apiClient } from "@/lib/api";
import type { OptionsIV, OptionsOverview, PutCallData } from "@/types/api";
import type { EChartsOption } from "echarts";

/** 先按候选键直接取数字；未命中再在同名嵌套对象内取（后端 /options/overview 为嵌套结构） */
function nestedNumber(root: unknown, outerKeys: string[], innerKeys: string[]): number | null {
  const direct = pickNumber(root, outerKeys);
  if (direct !== null) return direct;
  if (!root || typeof root !== "object") return null;
  const o = root as Record<string, unknown>;
  for (const k of outerKeys) {
    const v = o[k];
    if (v && typeof v === "object") {
      const n = pickNumber(v, innerKeys);
      if (n !== null) return n;
    }
  }
  return null;
}

interface IvPoint {
  tenor: string;
  iv: number;
}

/** IV 期限结构（iv 为比率时转换为百分数） */
function ivTermSeries(raw: unknown): IvPoint[] {
  let rows: unknown[] = [];
  if (Array.isArray(raw)) rows = raw;
  else if (raw && typeof raw === "object") {
    const o = raw as Record<string, unknown>;
    for (const k of ["term_structure", "tenors", "curve", "rows", "items"]) {
      if (Array.isArray(o[k])) {
        rows = o[k] as unknown[];
        break;
      }
    }
  }
  const out: IvPoint[] = [];
  for (const row of rows) {
    if (!row || typeof row !== "object") continue;
    const tenor = pickString(row, ["tenor", "term", "expiry", "date", "label", "name"]);
    const iv = pickNumber(row, ["iv", "implied_volatility", "value", "vol"]);
    if (tenor && iv !== null) out.push({ tenor, iv: iv <= 2 ? iv * 100 : iv });
  }
  return out;
}

interface PcPoint {
  time: string;
  volumeRatio: number | null;
  oiRatio: number | null;
}

/** Put/Call 历史序列（宽松行结构） */
function pcSeries(raw: unknown): PcPoint[] {
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
  const out: PcPoint[] = [];
  for (const row of rows) {
    if (!row || typeof row !== "object") continue;
    const time = pickString(row, ["date", "time", "day", "observation_time"]) ?? "";
    const volumeRatio = pickNumber(row, ["volume_ratio", "volume_pcr", "pcr"]);
    const oiRatio = pickNumber(row, ["oi_ratio", "oi_pcr", "open_interest_ratio"]);
    if (volumeRatio !== null || oiRatio !== null) out.push({ time, volumeRatio, oiRatio });
  }
  return out;
}

export default function OptionsPage() {
  const overviewApi = useApi<OptionsOverview>(() => apiClient.options.getOverview(), [], {
    refreshInterval: 120_000,
  });
  const ivApi = useApi<OptionsIV>(() => apiClient.options.getIV(), [], {
    refreshInterval: 120_000,
  });
  const pcApi = useApi<PutCallData>(() => apiClient.options.getPutCall(), [], {
    refreshInterval: 120_000,
  });

  const ov = overviewApi.data;
  const iv = ivApi.data;
  const pc = pcApi.data;

  const oiUsd = nestedNumber(ov, ["total_oi_usd", "total_oi", "open_interest", "oi"], [
    "total_usd",
    "total",
    "usd",
    "notional",
    "value",
    "oi",
  ]);
  const volUsd = nestedNumber(ov, ["volume_24h", "volume", "total_volume"], [
    "total_usd",
    "total",
    "usd",
    "notional",
    "value",
  ]);
  const pcr = nestedNumber(pc, ["volume_ratio", "volume_pcr", "put_call_ratio", "pcr"], [
    "value",
    "ratio",
    "volume_ratio",
    "current",
  ]);
  const pcrOi = nestedNumber(pc, ["oi_ratio", "open_interest_ratio"], ["value", "ratio", "current"]);
  const maxPain = pickNumber(ov, ["max_pain", "maxpain"]);

  const dvol = pickNumber(iv, ["dvol", "iv", "implied_volatility", "atm_iv"]);
  const skew25d = pickNumber(iv, ["skew_25d", "skew"]);
  const ivPercentile = pickNumber(iv, ["iv_percentile", "percentile"]);
  const termCurve = useMemo(() => ivTermSeries(iv), [iv]);
  const pcCurve = useMemo(() => pcSeries(pc), [pc]);

  /** Call 权重（用 volume ratio，缺省 oi ratio）：r=put/call -> call% = 1/(1+r) */
  const pcrForBar = pcr ?? pcrOi;
  const callPct = pcrForBar !== null && pcrForBar > 0 ? (1 / (1 + pcrForBar)) * 100 : null;

  const pcOption: EChartsOption = useMemo(() => {
    if (pcCurve.length === 0) return {};
    const series: EChartsOption["series"] = [];
    const hasVol = pcCurve.some((p) => p.volumeRatio !== null);
    const hasOi = pcCurve.some((p) => p.oiRatio !== null);
    if (hasVol) {
      series.push({
        type: "line",
        name: "成交量 P/C",
        data: pcCurve.map((p) => p.volumeRatio),
        showSymbol: false,
        lineStyle: { color: CHART.accent, width: 1.6 },
      });
    }
    if (hasOi) {
      series.push({
        type: "line",
        name: "持仓 P/C",
        data: pcCurve.map((p) => p.oiRatio),
        showSymbol: false,
        lineStyle: { color: CHART.warn, width: 1.4 },
      });
    }
    return {
      tooltip: {
        trigger: "axis",
        ...baseTooltip(),
        valueFormatter: (v) => (typeof v === "number" ? v.toFixed(3) : "—"),
      },
      legend: { show: hasVol && hasOi, textStyle: { color: CHART.text, fontSize: 10 }, top: 0 },
      grid: { left: 8, right: 12, top: 28, bottom: 0, containLabel: true },
      xAxis: {
        type: "category",
        data: pcCurve.map((p) => (p.time ? p.time.slice(0, 10) : "")),
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
      series,
    };
  }, [pcCurve]);

  const ivOption: EChartsOption = useMemo(() => {
    if (termCurve.length === 0) return {};
    return {
      tooltip: {
        trigger: "axis",
        ...baseTooltip(),
        valueFormatter: (v) => (typeof v === "number" ? `${v.toFixed(1)}%` : "—"),
      },
      grid: { left: 8, right: 12, top: 24, bottom: 0, containLabel: true },
      xAxis: {
        type: "category",
        data: termCurve.map((p) => p.tenor),
        boundaryGap: false,
        axisLine: { lineStyle: { color: CHART.axis } },
        axisTick: { show: false },
        axisLabel: { color: CHART.text, fontSize: 10 },
      },
      yAxis: {
        type: "value",
        scale: true,
        splitLine: { lineStyle: { color: CHART.grid } },
        axisLabel: { color: CHART.text, fontSize: 10, formatter: "{value}%" },
      },
      series: [
        {
          type: "line",
          data: termCurve.map((p) => p.iv),
          showSymbol: true,
          symbolSize: 6,
          lineStyle: { color: CHART.orange, width: 1.8 },
          itemStyle: { color: CHART.orange },
        },
      ],
    };
  }, [termCurve]);

  const anyLoading = overviewApi.loading && ivApi.loading && pcApi.loading;
  const hasAnyData = ov !== null || iv !== null || pc !== null;

  return (
    <div>
      <PageHeader
        title="期权"
        subtitle="BTC 期权市场概览 · Put/Call Ratio 与隐含波动率反映专业资金的情绪与预期波动"
        right={
          <QualityBadge
            status={overviewApi.meta?.quality_status ?? ivApi.meta?.quality_status ?? null}
          />
        }
      />

      {overviewApi.error && !hasAnyData && (
        <ErrorState
          error={overviewApi.error}
          lastUpdatedAt={overviewApi.lastUpdatedAt}
          onRetry={() => {
            overviewApi.refresh();
            ivApi.refresh();
            pcApi.refresh();
          }}
          className="mb-4"
        />
      )}

      {anyLoading && !hasAnyData ? (
        <LoadingSkeleton variant="card" />
      ) : (
        <div className="space-y-5">
          {/* 概览 */}
          <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <StatTile
              label="期权未平仓"
              value={oiUsd !== null ? formatUsd(oiUsd) : "—"}
              sub="全部未到期合约名义价值"
            />
            <StatTile
              label="24h 成交量"
              value={volUsd !== null ? formatUsd(volUsd) : "—"}
              sub="近 24 小时成交名义价值"
            />
            <StatTile
              label="Put/Call Ratio"
              value={pcr !== null ? pcr.toFixed(3) : "—"}
              tone={pcr !== null ? (pcr > 1 ? "down" : "up") : "default"}
              sub={pcrOi !== null ? `持仓比 ${pcrOi.toFixed(3)}` : "成交量口径；>1 偏防御"}
            />
            <StatTile
              label="Max Pain"
              value={maxPain !== null ? `$${formatPrice(maxPain)}` : "—"}
              sub="到期时让最多期权归零的价格"
            />
          </section>

          {/* Put/Call */}
          <section className="rounded-xl border border-line bg-bg-raised p-4">
            <SectionTitle right={<span className="text-[10px] text-muted">P/C 低=偏投机看涨，高=偏防御对冲</span>}>
              Put/Call Ratio
            </SectionTitle>

            {pcrForBar !== null ? (
              <div className="max-w-xl">
                <SplitBar
                  leftLabel={`Call ${(callPct ?? 0).toFixed(1)}%`}
                  rightLabel={`Put ${(100 - (callPct ?? 0)).toFixed(1)}%`}
                  leftPct={callPct ?? 50}
                />
              </div>
            ) : (
              <p className="text-xs text-muted">Put/Call 数据暂不可用（{pcApi.error?.message ?? "暂无数据"}）</p>
            )}

            {pcCurve.length > 0 ? (
              <div className="mt-4">
                <EChart option={pcOption} height={240} />
              </div>
            ) : (
              pcrForBar !== null && (
                <p className="mt-3 text-xs text-muted">P/C 历史序列暂不可用（{pcApi.error?.message ?? "暂无历史"}）</p>
              )
            )}
          </section>

          {/* IV */}
          <section className="rounded-xl border border-line bg-bg-raised p-4">
            <SectionTitle right={<span className="text-[10px] text-muted">期限结构：不同到期日的隐含波动率</span>}>
              隐含波动率 IV
            </SectionTitle>

            <div className="grid gap-3 sm:grid-cols-3">
              <StatTile
                label="DVOL / ATM IV"
                value={dvol !== null ? `${dvol <= 2 ? (dvol * 100).toFixed(1) : dvol.toFixed(1)}%` : "—"}
                sub="市场对未来 30 天波动的定价"
              />
              <StatTile
                label="25d Skew"
                value={skew25d !== null ? formatNumber(skew25d, 3) : "—"}
                sub={skew25d !== null && skew25d > 0 ? "看跌保护溢价（防御情绪）" : "看涨溢价（投机情绪）"}
              />
              <StatTile
                label="IV 历史分位"
                value={ivPercentile !== null ? `${Math.round(ivPercentile <= 1 ? ivPercentile * 100 : ivPercentile)}%` : "—"}
                sub="当前 IV 在自身历史中的位置"
              />
            </div>

            {termCurve.length > 0 ? (
              <div className="mt-4">
                <EChart option={ivOption} height={240} />
              </div>
            ) : (
              <p className="mt-3 text-xs text-muted">IV 期限结构暂不可用（{ivApi.error?.message ?? "暂无数据"}）</p>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
