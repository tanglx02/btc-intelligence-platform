/**
 * 首页（占位）。
 *
 * 仪表盘（价格横幅、Regime 状态条、9 维度状态卡、个人区域、系统健康摘要）
 * 将由下一任务实现。本页仅验证 Shell 布局与设计系统。
 */

import Link from "next/link";

const PLANNED_SECTIONS = [
  {
    key: "price",
    title: "价格横幅",
    desc: "BTC 实时价格、24H/7D/30D 涨跌与多源交叉验证",
    tone: "text-accent",
    icon: "₿",
  },
  {
    key: "regime",
    title: "Market Regime 状态条",
    desc: "一句话综合市场状态、置信度与「为什么？」证据链",
    tone: "text-up",
    icon: "◎",
  },
  {
    key: "dimensions",
    title: "9 大维度状态卡",
    desc: "市场阶段/趋势/估值/资金/链上/杠杆/宏观/情绪/风险",
    tone: "text-warn",
    icon: "⊞",
  },
  {
    key: "system",
    title: "系统健康摘要",
    desc: "Provider 状态统计、数据质量比例与最近故障切换",
    tone: "text-down",
    icon: "⛬",
  },
] as const;

export default function Home() {
  return (
    <div className="mx-auto max-w-3xl py-6 lg:py-10">
      {/* 欢迎区 */}
      <section className="animate-fade-up">
        <p className="font-mono text-[11px] uppercase tracking-[0.22em] text-accent">
          Smart Research Terminal
        </p>
        <h1 className="mt-2 text-2xl font-bold tracking-tight text-[#e6edf3] lg:text-3xl">
          BTC 全市场智能研究平台
        </h1>
        <p className="mt-3 max-w-xl text-sm leading-relaxed text-muted">
          全局布局、导航与数据基础设施已就绪。
          首页仪表盘（价格横幅、Regime 状态条、9 大维度状态卡、个人区域）将在下一任务实现 ——
          你可以通过左侧导航浏览全部 25 个页面的入口。
        </p>
      </section>

      {/* 规划区块 */}
      <section className="mt-8 grid gap-3 sm:grid-cols-2">
        {PLANNED_SECTIONS.map((s, i) => (
          <div
            key={s.key}
            className="animate-fade-up rounded-xl border border-line bg-bg-raised p-4 transition-colors hover:border-accent/40"
            style={{ animationDelay: `${120 + i * 90}ms` }}
          >
            <div className="flex items-center justify-between">
              <span aria-hidden className={`font-mono text-base ${s.tone}`}>
                {s.icon}
              </span>
              <span className="rounded border border-line px-1.5 py-0.5 text-[10px] text-muted">
                下一任务
              </span>
            </div>
            <h2 className="mt-2.5 text-sm font-semibold text-[#e6edf3]">{s.title}</h2>
            <p className="mt-1 text-xs leading-relaxed text-muted">{s.desc}</p>
          </div>
        ))}
      </section>

      {/* 引导 */}
      <section
        className="animate-fade-up mt-6 flex flex-col items-start gap-3 rounded-xl border border-dashed border-line px-4 py-4 sm:flex-row sm:items-center sm:justify-between"
        style={{ animationDelay: "480ms" }}
      >
        <p className="text-xs leading-relaxed text-muted">
          顶栏价格胶囊将在后端 WebSocket 推送后实时刷新；
          右上角指示灯反映全局数据健康状态。
        </p>
        <Link
          href="/market"
          className="shrink-0 rounded-lg border border-accent/40 bg-accent/10 px-3 py-1.5 text-xs font-medium text-accent transition-colors hover:bg-accent/20"
        >
          前往 BTC 行情 →
        </Link>
      </section>
    </div>
  );
}
