/**
 * 免责声明条 —— 所有模拟 / 回测页面强制展示（docs/architecture/15 §3.3、14 §4.3）。
 */

import type { ReactNode } from "react";

import { cn } from "./utils";

export function Disclaimer({
  children,
  className = "",
}: {
  children?: ReactNode;
  className?: string;
}) {
  return (
    <aside
      className={cn(
        "flex items-start gap-2.5 rounded-xl border border-dashed border-warn/35 bg-warn/[0.04] px-4 py-3",
        className,
      )}
    >
      <span aria-hidden className="mt-0.5 text-warn">
        ⚠
      </span>
      <p className="text-[11px] italic leading-relaxed text-warn/90">
        {children ??
          "模拟与回测结果基于历史数据与简化市场假设（手续费、滑点、成交价），仅供学习研究，不构成投资建议，不保证未来表现。"}
      </p>
    </aside>
  );
}
