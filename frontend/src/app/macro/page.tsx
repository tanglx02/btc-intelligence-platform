"use client";

/**
 * 宏观页：
 * - 宏观指标卡网格（DXY / 联邦利率 / 10Y / 实际收益率 / CPI / M2）
 * - 点击卡片切换该指标趋势曲线（近 2 年）
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
  type SeriesPoint,
} from "@/components/market/utils";
import { ApiRequestError, apiClient } from "@/lib/api";
import type { ApiResponse } from "@/types/api";
import type { EChartsOption } from "echarts";

const RANGE_DAYS = 730;

interface MacroDef {
  key: string;
  label: string;
  desc: string;
  /** pct: 收益率/通胀类（自动识别 0-1 比率），plain: 指数/数量类 */
  fmt: "pct" | "plain";
  digits: number;
}

const MACRO_DEFS: MacroDef[] = [
  { key: "dxy", label: "美元指数 DXY", desc: "美元相对一篮子主要货币的强弱，与风险资产通常负相关", fmt: "plain", digits: 2 },
  { key: "fed_rate", label: "联邦基金利率", desc: "美联储政策利率，流动性环境的锚", fmt: "pct", digits: 2 },
  { key: "treasury_10y", label: "10Y 国债收益率", desc: "美国 10 年期国债名义收益率", fmt: "pct", digits: 2 },
  { key: "real_yield", label: "10Y 实际收益率", desc: "剔除通胀后的无风险实际回报，抬升压制无息资产", fmt: "pct", digits: 2 },
  { key: "cpi", label: "CPI 同比", desc: "消费者价格指数同比增速（通胀）", fmt: "pct", digits: 1 },
  { key: "m2", label: "M2 货币供应", desc: "美国广义货币供应量，流动性总量", fmt: "plain", digits: 0 },
];

/** 收益率/通胀类：0-1 比率自动转百分数 */
function fmtMacroValue(v: number | null, def: MacroDef): string {
  if (v === null) return "—";
  if (def.fmt === "plain") return formatNumber(v, def.digits);
  const val = Math.abs(v) <= 1 ? v * 100 : v;
  return `${val.toFixed(def.digits)}%`;
}

export default function MacroPage() {
  // 六个序列批量拉取；单个序列失败不影响其余
  const macroApi = useApi<Record<string, SeriesPoint[]>>(
    async () => {
      const entries = await Promise.all(
        MACRO_DEFS.map(async (m) => {
          try {
            const res = await apiClient.macro.getSeries(m.key, {
              start: daysAgoISO(RANGE_DAYS),
              end: daysAgoISO(0),
            });
            return [m.key, extractSeriesRecords(res.data)] as const;
          } catch {
            // 单个序列失败不影响其余，全空时由下方 total 判断抛错
            return [m.key, [] as SeriesPoint[]] as const;
          }
        }),
      );
      const data = Object.fromEntries(entries) as Record<string, SeriesPoint[]>;
      const total = MACRO_DEFS.reduce((s, m) => s + (data[m.key]?.length ?? 0), 0);
      if (total === 0) {
        throw new ApiRequestError("宏观数据服务不可达", { code: "NETWORK_ERROR" });
      }
      return {
        success: true,
        data,
        meta: null,
      } as ApiResponse<Record<string, SeriesPoint[]>>;
    },
    [],
    { refreshInterval: 600_000 },
  );

  const [active, setActive] = useState("dxy");

  const cards = useMemo(() => {
    const data = macroApi.data ?? {};
    return MACRO_DEFS.map((def) => {
      const series = data[def.key] ?? [];
      const last = series.length > 0 ? series[series.length - 1] : null;
      const prev = series.length > 1 ? series[series.length - 2] : null;
      const value = last ? last.value : null;
      const prevValue = prev ? prev.value : null;
      const diff =
        value !== null && prevValue !== null && prevValue !== 0
          ? ((value - prevValue) / Math.abs(prevValue)) * 100
          : null;
      return { def, series, value, updated: last?.time ?? null, diff };
    });
  }, [macroApi.data]);

  const activeDef = MACRO_DEFS.find((m) => m.key === active) ?? MACRO_DEFS[0];
  const activeSeries = useMemo(
    () => (macroApi.data ?? {})[active] ?? [],
    [macroApi.data, active],
  );

  const chartOption: EChartsOption = useMemo(() => {
    if (activeSeries.length === 0) return {};
    const isPct = activeDef.fmt === "pct";
    return {
      tooltip: {
        trigger: "axis",
        ...baseTooltip(),
        valueFormatter: (v) => (typeof v === "number" ? fmtMacroValue(v, activeDef) : "—"),
      },
      grid: { left: 8, right: 12, top: 24, bottom: 0, containLabel: true },
      xAxis: {
        type: "category",
        data: activeSeries.map((p) => (p.time ? p.time.slice(0, 10) : "")),
        boundaryGap: false,
        axisLine: { lineStyle: { color: CHART.axis } },
        axisTick: { show: false },
        axisLabel: { color: CHART.text, fontSize: 10 },
      },
      yAxis: {
        type: "value",
        scale: true,
        splitLine: { lineStyle: { color: CHART.grid } },
        axisLabel: {
          color: CHART.text,
          fontSize: 10,
          formatter: (v: number) => (isPct ? `${v}%` : formatNumber(v, 1)),
        },
      },
      series: [
        {
          type: "line",
          data: activeSeries.map((p) => p.value),
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
  }, [activeSeries, activeDef]);

  const hasData = macroApi.data !== null;

  return (
    <div>
      <PageHeader
        title="宏观环境"
        subtitle="美元、利率、通胀与流动性 · 宏观条件决定风险偏好，是风险引擎的重要维度"
        right={
          <QualityBadge status={macroApi.meta?.quality_status ?? null} />
        }
      />

      {macroApi.error && !hasData && (
        <ErrorState
          error={macroApi.error}
          lastUpdatedAt={macroApi.lastUpdatedAt}
          onRetry={macroApi.refresh}
          className="mb-4"
        />
      )}

      {macroApi.loading && !hasData ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 6 }, (_, i) => (
            <LoadingSkeleton key={i} variant="card" />
          ))}
        </div>
      ) : (
        <>
          {/* 指标卡网格 */}
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {cards.map((card) => (
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
                <p className="text-[11px] font-medium uppercase tracking-wider text-muted">
                  {card.def.label}
                </p>
                <p className="mt-1.5 font-mono text-lg font-semibold tabular-nums text-[#e6edf3]">
                  {fmtMacroValue(card.value, card.def)}
                </p>
                <p className="mt-1.5 text-[11px] text-muted">
                  {card.updated ? `更新于 ${card.updated}` : "暂无数据"}
                  {card.diff !== null && (
                    <>
                      {" · "}
                      <span className={card.diff >= 0 ? "text-up" : "text-down"}>
                        环比 {card.diff >= 0 ? "+" : ""}
                        {card.diff.toFixed(2)}%
                      </span>
                    </>
                  )}
                </p>
              </button>
            ))}
          </div>

          {/* 趋势曲线 */}
          <section className="mt-5 rounded-xl border border-line bg-bg-raised p-4">
            <SectionTitle right={<span className="text-[10px] text-muted">{activeDef.desc}</span>}>
              {activeDef.label} · 近 2 年趋势
            </SectionTitle>
            {activeSeries.length === 0 ? (
              <p className="py-10 text-center text-xs text-muted">
                该序列暂无数据（可能尚未采集，或键名不在后端目录中）
              </p>
            ) : (
              <EChart option={chartOption} height={300} />
            )}
          </section>
        </>
      )}
    </div>
  );
}
