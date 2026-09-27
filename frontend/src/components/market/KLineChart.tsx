"use client";

/**
 * BTC K 线图（TradingView Lightweight Charts v4 CandlestickSeries）。
 * 页面通过 kit.tsx 的动态导入（ssr:false）使用。
 */

import { useEffect, useRef } from "react";
import {
  ColorType,
  CrosshairMode,
  createChart,
  type CandlestickData,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";

import type { Candle } from "@/types/api";

/** 任意时间表示 -> Unix 秒（UTCTimestamp）；统一转时间戳避免混用业务日 */
function toUnix(t: unknown): UTCTimestamp | null {
  if (typeof t === "number" && Number.isFinite(t)) {
    const ms = t > 1e12 ? t : t * 1000;
    return Math.floor(ms / 1000) as UTCTimestamp;
  }
  if (typeof t === "string" && t.trim() !== "") {
    // 纯日期串按 UTC 午夜解析，保持与其他时间戳同一坐标系
    const ms = Date.parse(t.length === 10 ? `${t}T00:00:00Z` : t);
    if (Number.isNaN(ms)) return null;
    return Math.floor(ms / 1000) as UTCTimestamp;
  }
  return null;
}

export function KLineChart({ data, height = 440 }: { data: Candle[]; height?: number }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const chart = createChart(el, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#8b949e",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "rgba(48, 54, 61, 0.45)" },
        horzLines: { color: "rgba(48, 54, 61, 0.45)" },
      },
      rightPriceScale: { borderColor: "#30363d" },
      timeScale: { borderColor: "#30363d", timeVisible: true, secondsVisible: false },
      crosshair: { mode: CrosshairMode.Normal },
    });

    const series = chart.addCandlestickSeries({
      upColor: "#3fb950",
      downColor: "#f85149",
      borderVisible: false,
      wickUpColor: "#3fb950",
      wickDownColor: "#f85149",
    });

    chartRef.current = chart;
    seriesRef.current = series;

    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    const series = seriesRef.current;
    if (!chart || !series || data.length === 0) return;

    const seen = new Set<number>();
    const points: CandlestickData[] = [];
    for (const c of data) {
      const raw = (c.time ?? c["timestamp"]) as unknown;
      const time = toUnix(raw);
      if (time === null || seen.has(time)) continue;
      if (![c.open, c.high, c.low, c.close].every((n) => Number.isFinite(n))) continue;
      seen.add(time);
      points.push({ time, open: c.open, high: c.high, low: c.low, close: c.close });
    }
    points.sort((a, b) => (a.time as number) - (b.time as number));
    if (points.length === 0) return;

    series.setData(points);
    chart.timeScale().fitContent();
  }, [data]);

  return <div ref={containerRef} style={{ height }} className="w-full" />;
}

export default KLineChart;
