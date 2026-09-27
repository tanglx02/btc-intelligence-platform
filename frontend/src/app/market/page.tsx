"use client";

/**
 * BTC 行情页：K 线（Lightweight Charts）+ 周期切换 + 成交量柱状图（ECharts）
 * + 24h 统计卡。
 */

import { useMemo, useState } from "react";

import { ErrorState } from "@/components/common/ErrorState";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";
import {
  EChart,
  KLineChart,
  PageHeader,
  SectionTitle,
  StatTile,
} from "@/components/market/kit";
import { useApi } from "@/hooks/useApi";
import {
  baseTooltip,
  CHART,
  formatPct,
  formatPrice,
  formatUsd,
  shortDate,
} from "@/components/market/utils";
import { apiClient } from "@/lib/api";
import type { Candle, MarketStats, OhlcvParams, PriceData } from "@/types/api";
import type { EChartsOption } from "echarts";

const INTERVALS: { key: OhlcvParams["interval"]; label: string }[] = [
  { key: "1h", label: "1小时" },
  { key: "4h", label: "4小时" },
  { key: "1d", label: "1天" },
  { key: "1w", label: "1周" },
];

const INTERVAL_LIMIT: Record<string, number> = { "1h": 500, "4h": 500, "1d": 365, "1w": 260 };

export default function MarketPage() {
  const [interval, setInterval] = useState<NonNullable<OhlcvParams["interval"]>>("1d");

  const candlesApi = useApi<Candle[]>(
    () => apiClient.market.getOhlcv({ interval, limit: INTERVAL_LIMIT[interval] ?? 500 }),
    [interval],
    { refreshInterval: interval === "1h" ? 60_000 : 300_000 },
  );
  const statsApi = useApi<MarketStats>(() => apiClient.market.getStats(), [], {
    refreshInterval: 60_000,
  });
  const priceApi = useApi<PriceData>(() => apiClient.market.getPrice(), [], {
    refreshInterval: 30_000,
  });

  const candles = useMemo(() => candlesApi.data ?? [], [candlesApi.data]);

  /** 成交量序列（与 K 线同一时间轴） */
  const volumeSeries = useMemo(() => {
    const out: { time: string; volume: number; up: boolean }[] = [];
    for (const c of candles) {
      const raw: unknown = c.time ?? c["timestamp"];
      let time = "";
      if (typeof raw === "string") {
        time = shortDate(raw);
      } else if (typeof raw === "number" && Number.isFinite(raw)) {
        const ms = raw > 1e12 ? raw : raw * 1000;
        time = shortDate(new Date(ms).toISOString());
      }
      if (time === "" || c.volume === null || c.volume === undefined) continue;
      out.push({ time, volume: c.volume, up: c.close >= c.open });
    }
    return out;
  }, [candles]);

  const volumeOption: EChartsOption = useMemo(() => {
    return {
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        ...baseTooltip(),
        valueFormatter: (v) => formatUsd(typeof v === "number" ? v : null),
      },
      grid: { left: 8, right: 8, top: 16, bottom: 0, containLabel: true },
      xAxis: {
        type: "category",
        data: volumeSeries.map((p) => p.time),
        axisLine: { lineStyle: { color: CHART.axis } },
        axisTick: { show: false },
        axisLabel: { color: CHART.text, fontSize: 10 },
      },
      yAxis: {
        type: "value",
        splitLine: { lineStyle: { color: CHART.grid } },
        axisLabel: {
          color: CHART.text,
          fontSize: 10,
          formatter: (v: number) => formatUsd(v, 1),
        },
      },
      series: [
        {
          type: "bar",
          data: volumeSeries.map((p) => ({
            value: p.volume,
            itemStyle: { color: p.up ? "rgba(63,185,80,0.75)" : "rgba(248,81,73,0.75)" },
          })),
          barMaxWidth: 10,
        },
      ],
    };
  }, [volumeSeries]);

  const stats = statsApi.data;
  const priceData = priceApi.data;
  const lastCandle = candles.length > 0 ? candles[candles.length - 1] : null;
  const prevCandle = candles.length > 1 ? candles[candles.length - 2] : null;
  const changePct =
    priceData?.change_24h_pct ??
    (lastCandle && prevCandle && prevCandle.close !== 0
      ? (lastCandle.close / prevCandle.close - 1) * 100
      : null);

  // MarketStats 未显式声明 high_24h/low_24h（索引签名字段），需显式类型守卫
  const high24 =
    typeof stats?.high_24h === "number"
      ? stats.high_24h
      : typeof priceData?.high_24h === "number"
        ? priceData.high_24h
        : null;
  const low24 =
    typeof stats?.low_24h === "number"
      ? stats.low_24h
      : typeof priceData?.low_24h === "number"
        ? priceData.low_24h
        : null;
  const volume24 = stats?.volume_24h ?? null;
  const loading = candlesApi.loading && candles.length === 0;

  return (
    <div>
      <PageHeader
        title="BTC 行情"
        subtitle="K 线、成交量与关键市场统计 · 本地库优先，缺失区间自动回源补齐"
      />

      {/* 周期切换 */}
      <div className="mb-4 flex items-center gap-1.5">
        {INTERVALS.map((it) => (
          <button
            key={it.key}
            type="button"
            onClick={() => setInterval(it.key as NonNullable<OhlcvParams["interval"]>)}
            aria-pressed={interval === it.key}
            className={`rounded-md border px-3 py-1.5 text-xs font-medium transition-colors ${
              interval === it.key
                ? "border-accent/50 bg-accent/15 text-accent"
                : "border-line bg-bg-raised text-muted hover:border-accent/30 hover:text-[#c9d1d9]"
            }`}
          >
            {it.label}
          </button>
        ))}
        {candlesApi.error && candles.length > 0 && (
          <span className="ml-2 text-[11px] text-warn">K 线数据暂时无法更新，显示缓存数据</span>
        )}
      </div>

      {/* K 线 */}
      {loading ? (
        <LoadingSkeleton className="!items-stretch" variant="card" />
      ) : candles.length === 0 ? (
        <ErrorState
          error={candlesApi.error ?? undefined}
          lastUpdatedAt={candlesApi.lastUpdatedAt}
          onRetry={candlesApi.refresh}
        >
          <p className="text-xs text-muted">暂无 K 线数据（{interval} 周期）</p>
        </ErrorState>
      ) : (
        <div className="rounded-xl border border-line bg-bg-raised p-3">
          <KLineChart data={candles} height={440} />
        </div>
      )}

      {/* 成交量 */}
      {volumeSeries.length > 0 && (
        <div className="mt-4 rounded-xl border border-line bg-bg-raised p-3">
          <SectionTitle>成交量（USD）</SectionTitle>
          <EChart option={volumeOption} height={200} />
        </div>
      )}

      {/* 24h 统计 */}
      <section className="mt-4">
        <SectionTitle>24 小时统计</SectionTitle>
        {statsApi.loading && !stats && priceApi.loading && !priceData ? (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {Array.from({ length: 4 }, (_, i) => (
              <LoadingSkeleton key={i} variant="stat" />
            ))}
          </div>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <StatTile
              label="最高价"
              value={high24 !== null ? `$${formatPrice(high24)}` : "—"}
              tone="up"
            />
            <StatTile
              label="最低价"
              value={low24 !== null ? `$${formatPrice(low24)}` : "—"}
              tone="down"
            />
            <StatTile
              label="成交量"
              value={formatUsd(volume24)}
              sub={stats?.vwap != null ? `VWAP $${formatPrice(stats.vwap)}` : undefined}
            />
            <StatTile
              label="24h 涨跌"
              value={<span>{changePct !== null ? formatPct(changePct) : "—"}</span>}
              tone={changePct !== null ? (changePct >= 0 ? "up" : "down") : "default"}
              sub={stats?.volatility_24h != null ? `波动率 ${formatPct(stats.volatility_24h)}` : undefined}
            />
          </div>
        )}

        {(statsApi.error || priceApi.error) && !stats && !priceData && (
          <ErrorState
            error={statsApi.error ?? priceApi.error}
            lastUpdatedAt={statsApi.lastUpdatedAt ?? priceApi.lastUpdatedAt}
            onRetry={() => {
              statsApi.refresh();
              priceApi.refresh();
            }}
            className="mt-3"
          />
        )}
      </section>
    </div>
  );
}
