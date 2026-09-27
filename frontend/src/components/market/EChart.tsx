"use client";

/**
 * ECharts 通用包装：初始化 / 自适应尺寸 / option 更新 / 卸载销毁。
 * 页面通过 kit.tsx 的动态导入（ssr:false）使用，避免 SSR 问题。
 */

import { useEffect, useRef } from "react";
import * as echarts from "echarts";
import type { EChartsOption } from "echarts";

export interface EChartProps {
  option: EChartsOption;
  height?: number;
  className?: string;
}

export function EChart({ option, height = 320, className = "" }: EChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const chart = echarts.init(el);
    chartRef.current = chart;

    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(el);

    return () => {
      observer.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    chartRef.current?.setOption(option, true);
  }, [option]);

  return <div ref={containerRef} style={{ height }} className={`w-full ${className}`} />;
}

export default EChart;
