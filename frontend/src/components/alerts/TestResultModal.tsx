"use client";

/**
 * 「测试规则」结果弹窗：立即求值一次，展示命中结论 + 各子条件详情 + 证据 + 上下文快照。
 */

import { EvalResultBadge } from "./badges";
import { ConditionResultTree, ContextSnapshot, EvidenceDetailList } from "./ResultDetails";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";
import type { RuleTestResult } from "./types";
import { Banner, Modal } from "./ui";

interface TestResultModalProps {
  open: boolean;
  onClose: () => void;
  loading?: boolean;
  error?: string | null;
  result?: RuleTestResult | null;
}

/** 大结论横幅 */
function VerdictBanner({ result }: { result: RuleTestResult }) {
  if (result.result === "TRUE") {
    return (
      <div className="animate-fade-up flex items-center gap-2.5 rounded-lg border border-up/40 bg-up/10 px-3 py-2.5">
        <span aria-hidden className="text-lg text-up">✅</span>
        <div>
          <p className="text-sm font-semibold text-up">当前满足，规则会立即触发</p>
          <p className="mt-0.5 text-[11px] text-muted">
            注意：实际触发还受持续时间、连续次数与冷却期约束。
          </p>
        </div>
      </div>
    );
  }
  if (result.result === "UNKNOWN") {
    return (
      <div className="animate-fade-up flex items-center gap-2.5 rounded-lg border border-warn/40 bg-warn/10 px-3 py-2.5">
        <span aria-hidden className="text-lg text-warn">❓</span>
        <div>
          <p className="text-sm font-semibold text-warn">数据不足，无法判定</p>
          <p className="mt-0.5 text-[11px] text-muted">
            部分指标缺失或过期时按 UNKNOWN 处理，规则不会触发（避免误报）。
          </p>
        </div>
      </div>
    );
  }
  return (
    <div className="animate-fade-up flex items-center gap-2.5 rounded-lg border border-line bg-bg-hover/60 px-3 py-2.5">
      <span aria-hidden className="text-lg">❌</span>
      <div>
        <p className="text-sm font-semibold text-[#e6edf3]">当前不满足</p>
        <p className="mt-0.5 text-[11px] text-muted">条件暂未命中，规则保持静默观察。</p>
      </div>
    </div>
  );
}

export function TestResultModal({ open, onClose, loading, error, result }: TestResultModalProps) {
  return (
    <Modal open={open} onClose={onClose} title="规则测试结果" widthClass="max-w-2xl">
      {loading && (
        <LoadingSkeleton variant="lines" rows={5} />
      )}

      {!loading && error && <Banner tone="error">{error}</Banner>}

      {!loading && !error && result && !result.found && (
        <Banner tone="error">规则不存在或已被删除</Banner>
      )}

      {!loading && !error && result && result.found && (
        <div className="flex flex-col gap-4">
          <VerdictBanner result={result} />

          <section>
            <h3 className="mb-2 flex items-center justify-between text-[11px] font-medium uppercase tracking-wider text-muted">
              子条件详情
              <EvalResultBadge result={result.result} />
            </h3>
            <ConditionResultTree details={result.condition_result} />
          </section>

          <section>
            <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wider text-muted">证据链</h3>
            <EvidenceDetailList evidence={result.evidence} />
          </section>

          <section>
            <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wider text-muted">
              当前上下文快照
            </h3>
            <ContextSnapshot snapshot={result.context_snapshot} />
          </section>
        </div>
      )}
    </Modal>
  );
}
