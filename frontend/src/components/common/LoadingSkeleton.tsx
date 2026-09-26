/**
 * 加载骨架屏（shimmer 动画）。
 */

interface LoadingSkeletonProps {
  /** 骨架形态：卡片 / 文本行 / 指标块 */
  variant?: "card" | "lines" | "stat" | "table";
  /** 行数 / 个数 */
  rows?: number;
  className?: string;
}

function Shimmer({ className = "" }: { className?: string }) {
  return (
    <div
      className={`relative overflow-hidden rounded-md bg-bg-hover/80 ${className}`}
      aria-hidden
    >
      <div className="absolute inset-0 -translate-x-full animate-shimmer bg-gradient-to-r from-transparent via-white/[0.06] to-transparent" />
    </div>
  );
}

export function LoadingSkeleton({ variant = "card", rows = 3, className = "" }: LoadingSkeletonProps) {
  if (variant === "stat") {
    return (
      <div className={`flex flex-col gap-3 ${className}`} role="status" aria-label="加载中">
        <Shimmer className="h-3 w-20" />
        <Shimmer className="h-8 w-32" />
        <Shimmer className="h-3 w-24" />
      </div>
    );
  }

  if (variant === "lines") {
    return (
      <div className={`flex flex-col gap-2.5 ${className}`} role="status" aria-label="加载中">
        {Array.from({ length: rows }, (_, i) => (
          <Shimmer key={i} className={`h-3.5 ${i === rows - 1 ? "w-2/5" : "w-full"}`} />
        ))}
      </div>
    );
  }

  if (variant === "table") {
    return (
      <div className={`flex flex-col gap-2 ${className}`} role="status" aria-label="加载中">
        {Array.from({ length: rows }, (_, i) => (
          <div key={i} className="flex items-center gap-3">
            <Shimmer className="h-8 flex-1" />
            <Shimmer className="h-8 w-24" />
            <Shimmer className="h-8 w-16" />
          </div>
        ))}
      </div>
    );
  }

  // card：标题 + 数值 + 辅助行
  return (
    <div
      className={`flex flex-col gap-4 rounded-xl border border-line bg-bg-raised p-5 ${className}`}
      role="status"
      aria-label="加载中"
    >
      <Shimmer className="h-3.5 w-24" />
      <Shimmer className="h-7 w-36" />
      <Shimmer className="h-3 w-full" />
      <Shimmer className="h-3 w-3/5" />
    </div>
  );
}
