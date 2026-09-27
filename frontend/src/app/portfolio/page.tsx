"use client";

/**
 * 我的资产 —— 个人资产账本（docs/architecture/15 §2、16 §8.15）。
 *
 * 持仓概览（总投入/持仓/成本/市值/浮盈亏/收益率/回撤）→ 资产曲线（市值 vs 累计投入）
 * → 交易记录（买绿卖红）→ 手工记录交易 / 删除。
 * 规则：手工记录模式，绝不自动交易；交易所同步为预留功能（默认关闭）。
 */

import { useMemo, useState } from "react";

import { EquityCurveChart } from "@/components/portfolio/charts";
import {
  ConfirmDialog,
  GhostButton,
  Modal,
  PrimaryButton,
} from "@/components/portfolio/Modal";
import {
  DateInput,
  Field,
  FormError,
  NumberInput,
} from "@/components/portfolio/Form";
import { Badge, PageHeader, SectionCard, StatCard } from "@/components/portfolio/StatCard";
import { ToastHost, useToast } from "@/components/portfolio/toast";
import { useApi } from "@/hooks/useApi";
import { apiClient, type ApiRequestError } from "@/lib/api";
import type { Transaction } from "@/types/api";
import { ErrorState } from "@/components/common/ErrorState";
import { EmptyState } from "@/components/common/EmptyState";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";
import { TrendIndicator } from "@/components/common/TrendIndicator";
import {
  formatBtc,
  formatDateTime,
  formatMoney,
  pickNum,
  pickStr,
  sideMeta,
} from "@/components/portfolio/utils";

/* -------------------------------------------------------------------------- */
/* 记录交易表单                                                                */
/* -------------------------------------------------------------------------- */

type Side = "BUY" | "SELL";

function TransactionFormModal({
  open,
  onClose,
  onSaved,
}: {
  open: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [side, setSide] = useState<Side>("BUY");
  const [tradedAt, setTradedAt] = useState("");
  const [price, setPrice] = useState("");
  const [quantity, setQuantity] = useState("");
  const [fee, setFee] = useState("");
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  const validate = () => {
    const errs: Record<string, string> = {};
    if (!tradedAt) errs.tradedAt = "请选择成交时间";
    if (!price || Number(price) <= 0) errs.price = "成交价需大于 0";
    if (!quantity || Number(quantity) <= 0) errs.quantity = "BTC 数量需大于 0";
    setFieldErrors(errs);
    return Object.keys(errs).length === 0;
  };

  const submit = async () => {
    if (!validate()) return;
    setSubmitting(true);
    setError(null);
    try {
      await apiClient.portfolio.createTransaction({
        side,
        price: Number(price),
        quantity: Number(quantity),
        fee: fee ? Number(fee) : 0,
        traded_at: new Date(tradedAt).toISOString(),
        note: note.trim() || undefined,
        source: "MANUAL",
      });
      toast.show("success", side === "BUY" ? "买入记录已入账" : "卖出记录已入账");
      onSaved();
      onClose();
    } catch (err) {
      const msg = err instanceof Error ? err.message : "入账失败，请稍后重试";
      setError(msg);
      toast.show("error", msg);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="记录一笔交易"
      description="手工录入真实成交（账本只增改留痕）。模拟成交由定投模拟产生，与此分列。"
      footer={
        <>
          <GhostButton onClick={onClose}>取消</GhostButton>
          <PrimaryButton loading={submitting} onClick={submit}>
            入账
          </PrimaryButton>
        </>
      }
    >
      <div className="space-y-4">
        <FormError message={error} />
        <Field label="方向" required>
          <div className="grid grid-cols-2 gap-2">
            {(["BUY", "SELL"] as Side[]).map((s) => {
              const active = side === s;
              const meta = sideMeta(s);
              return (
                <button
                  key={s}
                  type="button"
                  onClick={() => setSide(s)}
                  className={`rounded-lg border px-3 py-2 text-xs font-medium transition-colors ${
                    active ? meta.className : "border-line text-muted hover:bg-bg-hover"
                  }`}
                >
                  {s === "BUY" ? "买入（绿）" : "卖出（红）"}
                </button>
              );
            })}
          </div>
        </Field>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="成交时间" required error={fieldErrors.tradedAt}>
            <DateInput
              type="datetime-local"
              value={tradedAt}
              onChange={(e) => setTradedAt(e.target.value)}
            />
          </Field>
          <Field label="成交价（CNY）" required error={fieldErrors.price}>
            <NumberInput value={price} onChange={(e) => setPrice(e.target.value)} placeholder="如 685000" />
          </Field>
          <Field label="BTC 数量" required error={fieldErrors.quantity}>
            <NumberInput
              value={quantity}
              onChange={(e) => setQuantity(e.target.value)}
              step="0.00000001"
              placeholder="0.01000000"
            />
          </Field>
          <Field label="手续费" hint="可选">
            <NumberInput value={fee} onChange={(e) => setFee(e.target.value)} placeholder="0" />
          </Field>
          <Field label="备注" className="sm:col-span-2">
            <input
              type="text"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              maxLength={120}
              placeholder="如：交易所 A · 第一次买入"
              className="w-full rounded-lg border border-line bg-bg px-3 py-2 text-sm text-[#e6edf3] placeholder:text-muted/50 outline-none transition-colors focus:border-accent/60"
            />
          </Field>
        </div>
        <p className="text-[10px] leading-relaxed text-muted/70">
          当前为手工记录模式；交易所只读同步为未来预留功能（默认关闭）。平均成本采用 DCA 口径：Σ买入金额（含费）/ Σ买入数量，卖出按移动加权平均冲减。
        </p>
      </div>
    </Modal>
  );
}

/* -------------------------------------------------------------------------- */
/* 页面                                                                        */
/* -------------------------------------------------------------------------- */

export default function PortfolioPage() {
  const toast = useToast();
  const holdings = useApi(() => apiClient.portfolio.getHoldings(), []);
  const performance = useApi(() => apiClient.portfolio.getPerformance(), []);
  const transactions = useApi(() => apiClient.portfolio.getTransactions({ page_size: 100 }), []);

  const [formOpen, setFormOpen] = useState(false);
  const [deleting, setDeleting] = useState<Transaction | null>(null);
  const [actionLoading, setActionLoading] = useState(false);

  const h = holdings.data;
  const p = performance.data;
  const txs = useMemo(() => transactions.data ?? [], [transactions.data]);

  const currency = (pickStr(h ?? {}, ["currency"]) ?? "CNY").toUpperCase() === "USD" ? "USD" : "CNY";

  const refreshAll = () => {
    holdings.refresh();
    performance.refresh();
    transactions.refresh();
  };

  const confirmDelete = async () => {
    if (!deleting) return;
    setActionLoading(true);
    try {
      await apiClient.portfolio.deleteTransaction(deleting.id);
      toast.show("success", "交易记录已删除");
      setDeleting(null);
      refreshAll();
    } catch (err) {
      toast.show("error", err instanceof Error ? err.message : "删除失败");
    } finally {
      setActionLoading(false);
    }
  };

  const equityCurve = (p?.equity_curve ?? []).map((pt) => ({
    date: pt.date,
    value: pt.value,
    invested: pt.invested,
  }));

  const pnl = pickNum(h ?? {}, ["unrealized_pnl"]);
  const pnlPct = pickNum(h ?? {}, ["unrealized_pnl_pct"]);

  return (
    <div className="mx-auto max-w-5xl space-y-6 py-6 lg:py-8">
      <PageHeader
        index="02"
        kicker="Personal Ledger · Holdings"
        title="我的资产"
        description="个人 BTC 资产账本：手工记录每一笔真实成交，账本与计划严格分离。市值按最新聚合参考价估算，一切数字可溯源到账本行。"
        action={
          <>
            <GhostButton onClick={refreshAll}>刷新</GhostButton>
            <PrimaryButton onClick={() => setFormOpen(true)}>+ 记录交易</PrimaryButton>
          </>
        }
      />

      <ToastHost toast={toast.toast} />

      {/* 持仓概览 */}
      {holdings.loading && !h ? (
        <div className="grid gap-3 sm:grid-cols-4">
          {[0, 1, 2, 3].map((i) => (
            <LoadingSkeleton key={i} variant="card" />
          ))}
        </div>
      ) : holdings.error && !h ? (
        <ErrorState error={holdings.error as ApiRequestError} onRetry={holdings.refresh} />
      ) : h ? (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatCard label="累计投入" value={formatMoney(pickNum(h, ["invested_total"]), currency)} />
          <StatCard
            label="当前持仓"
            value={formatBtc(pickNum(h, ["btc_amount"]))}
            sub={pickNum(h, ["btc_amount"]) ? undefined : "暂无持仓"}
          />
          <StatCard label="平均成本" value={formatMoney(pickNum(h, ["avg_cost"]), currency)} sub="DCA 口径（含费）" />
          <StatCard
            label="当前市值"
            value={formatMoney(pickNum(h, ["market_value"]), currency)}
            sub={h.updated_at ? `估值更新 ${formatDateTime(h.updated_at)}` : undefined}
          />
          <StatCard
            label="浮动盈亏"
            value={formatMoney(pnl, currency, { sign: true })}
            tone={pnl !== null && pnl > 0 ? "up" : pnl !== null && pnl < 0 ? "down" : "muted"}
            sub={<TrendIndicator value={pnlPct} size="xs" />}
          />
          <StatCard
            label="累计收益率"
            value={<TrendIndicator value={pickNum(p ?? {}, ["total_return_pct"])} size="sm" showSign={false} />}
            sub="含已实现盈亏"
          />
          <StatCard
            label="最大回撤"
            value={pickNum(p ?? {}, ["max_drawdown_pct"]) !== null ? `${pickNum(p ?? {}, ["max_drawdown_pct"])?.toFixed(2)}%` : "—"}
            tone="warn"
            sub="基于每日快照序列"
          />
          <StatCard
            label="Sharpe"
            value={pickNum(p ?? {}, ["sharpe"]) !== null ? pickNum(p ?? {}, ["sharpe"])?.toFixed(2) : "—"}
            tone="accent"
            sub="日频年化（快照口径）"
          />
        </div>
      ) : null}

      {/* 资产曲线 */}
      <SectionCard title="资产曲线" extra={<span className="text-[10px] text-muted">市值 vs 累计投入</span>}>
        {performance.loading && !performance.data ? (
          <LoadingSkeleton variant="card" />
        ) : performance.error && !performance.data ? (
          <ErrorState error={performance.error as ApiRequestError} onRetry={performance.refresh} compact />
        ) : equityCurve.length ? (
          <EquityCurveChart data={equityCurve} currency={currency} height={300} />
        ) : (
          <EmptyState
            title="暂无快照数据"
            description="每日收盘后生成资产快照；记录第一笔交易后即可看到曲线。"
          />
        )}
      </SectionCard>

      {/* 交易记录 */}
      <SectionCard
        title="交易记录"
        extra={<span className="text-[10px] text-muted">买绿卖红 · 手工录入</span>}
        bodyClassName="p-0"
      >
        {transactions.loading && !txs.length ? (
          <div className="p-4">
            <LoadingSkeleton variant="table" rows={4} />
          </div>
        ) : transactions.error && !txs.length ? (
          <div className="p-4">
            <ErrorState error={transactions.error as ApiRequestError} onRetry={transactions.refresh} compact />
          </div>
        ) : !txs.length ? (
          <div className="p-4">
            <EmptyState
              title="还没有交易记录"
              description="把你真实买入/卖出的成交记录录入账本，系统将据此计算成本、市值与收益。"
              action={<PrimaryButton onClick={() => setFormOpen(true)}>记录第一笔交易</PrimaryButton>}
            />
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[640px] text-left text-xs">
              <thead>
                <tr className="border-b border-line text-[10px] uppercase tracking-wider text-muted">
                  <th className="px-4 py-2.5 font-medium">时间</th>
                  <th className="px-3 py-2.5 font-medium">方向</th>
                  <th className="px-3 py-2.5 text-right font-medium">价格</th>
                  <th className="px-3 py-2.5 text-right font-medium">数量（BTC）</th>
                  <th className="px-3 py-2.5 text-right font-medium">金额</th>
                  <th className="px-3 py-2.5 text-right font-medium">手续费</th>
                  <th className="px-4 py-2.5 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {txs.map((tx) => {
                  const meta = sideMeta(tx.side);
                  const amount = pickNum(tx as unknown as Record<string, unknown>, ["quote_amount", "amount"]);
                  return (
                    <tr key={tx.id} className="border-b border-line/40 transition-colors last:border-0 hover:bg-bg-hover/40">
                      <td className="px-4 py-2.5 font-mono tabular-nums text-muted">{formatDateTime(tx.traded_at)}</td>
                      <td className="px-3 py-2.5">
                        <Badge className={meta.className}>{meta.label}</Badge>
                      </td>
                      <td className="px-3 py-2.5 text-right font-mono tabular-nums">{formatMoney(tx.price)}</td>
                      <td className="px-3 py-2.5 text-right font-mono tabular-nums">{tx.quantity?.toFixed(8)}</td>
                      <td className="px-3 py-2.5 text-right font-mono tabular-nums">
                        {amount !== null ? formatMoney(amount) : "—"}
                      </td>
                      <td className="px-3 py-2.5 text-right font-mono tabular-nums text-muted">
                        {tx.fee ? formatMoney(tx.fee) : "—"}
                      </td>
                      <td className="px-4 py-2.5 text-right">
                        <GhostButton danger onClick={() => setDeleting(tx)}>
                          删除
                        </GhostButton>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </SectionCard>

      <p className="text-center text-[10px] leading-relaxed text-muted/70">
        账本只增改留痕（软删除 + 审计日志）；模拟交易与真实持仓在所有汇总中默认分列。
      </p>

      {formOpen && (
        <TransactionFormModal open={formOpen} onClose={() => setFormOpen(false)} onSaved={refreshAll} />
      )}

      <ConfirmDialog
        open={deleting !== null}
        title="删除交易记录"
        description={`确定删除 ${formatDateTime(deleting?.traded_at)} 的${sideMeta(deleting?.side).label}记录（${formatMoney(deleting?.price)} × ${deleting?.quantity?.toFixed(8) ?? "—"} BTC）？历史快照不重算，除非显式触发重算。`}
        loading={actionLoading}
        onConfirm={confirmDelete}
        onCancel={() => setDeleting(null)}
      />
    </div>
  );
}
