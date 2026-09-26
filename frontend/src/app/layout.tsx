import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "BTC 全市场智能研究平台",
  description:
    "BTC 全市场智能研究、历史数据、周期分析、个人资金计划、策略回测与风险监测平台",
};

/**
 * 根布局（占位骨架）。
 * 全局字体、主题 Provider、导航与页脚将在前端设计阶段接入。
 */
export default function RootLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
