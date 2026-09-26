import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import { AppShell } from "@/components/layout/AppShell";
import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "BTC 全市场智能研究平台",
    template: "%s · BTC 全市场智能研究平台",
  },
  description:
    "BTC 全市场智能研究、历史数据、周期分析、个人资金计划、策略回测与风险监测平台",
};

export const viewport: Viewport = {
  themeColor: "#0d1117",
};

export default function RootLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body className="bg-bg font-sans text-[#e6edf3] antialiased">
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
