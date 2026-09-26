/**
 * 数据质量徽标：VERIFIED 绿 / ESTIMATED 蓝 / STALE 黄 / CONFLICT 红 / INVALID 灰。
 * 视觉契约见 docs/architecture/16-page-architecture.md 第 6.3 节。
 */

import type { QualityStatus } from "@/types/api";

const QUALITY_META: Record<QualityStatus, { label: string; className: string; hint: string }> = {
  VERIFIED: {
    label: "已验证",
    className: "border-up/40 bg-up/10 text-up",
    hint: "多源交叉验证通过",
  },
  ESTIMATED: {
    label: "估算",
    className: "border-accent/40 bg-accent/10 text-accent",
    hint: "基于其他数据推算",
  },
  STALE: {
    label: "延迟",
    className: "border-warn/40 bg-warn/10 text-warn",
    hint: "数据暂时无法更新，展示最近可信值",
  },
  CONFLICT: {
    label: "冲突",
    className: "border-down/50 bg-down/10 text-down",
    hint: "数据源存在异常差异，结果仅供参考",
  },
  INVALID: {
    label: "无效",
    className: "border-muted/40 bg-muted/10 text-muted",
    hint: "数据校验失败或数据源未配置",
  },
};

interface QualityBadgeProps {
  status?: QualityStatus | null;
  /** 展示原始英文枚举而非中文标签 */
  raw?: boolean;
  className?: string;
}

export function QualityBadge({ status, raw = false, className = "" }: QualityBadgeProps) {
  if (!status) return null;
  const meta = QUALITY_META[status];
  if (!meta) return null;

  return (
    <span
      title={meta.hint}
      className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-medium leading-none tracking-wide ${meta.className} ${className}`}
    >
      <span aria-hidden className="h-1 w-1 rounded-full bg-current" />
      {raw ? status : meta.label}
    </span>
  );
}
