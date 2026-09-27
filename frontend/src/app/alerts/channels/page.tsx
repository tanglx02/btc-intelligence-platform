"use client";

/**
 * 通知渠道与摘要（/alerts/channels）：
 *
 * - SMTP 邮件配置卡（SmtpSection：GET/PUT /alerts/smtp/config + POST /alerts/smtp/test）
 * - 每日摘要 / 周报（DigestSections：GET/PUT /alerts/digest/config + POST /alerts/digest/send）
 * - 渠道列表（GET /alerts/channels + POST /channels/{id}/test）
 *
 * 表单区组件以 cfg 惰性初始化（见 components/alerts/ChannelFormSections.tsx），
 * 本页负责数据获取、加载/错误态与渠道列表。
 */

import { useState } from "react";

import { apiClient } from "@/lib/api";
import { useApi } from "@/hooks/useApi";
import { ErrorState } from "@/components/common/ErrorState";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";

import { DigestSections, SmtpSection, type BannerTone } from "@/components/alerts/ChannelFormSections";
import { formatDateTime } from "@/components/alerts/format";
import { Banner, PageHeader, cardCls } from "@/components/alerts/ui";
import { asAlert } from "@/components/alerts/types";
import type {
  ChannelListPayload,
  ChannelRecord,
  DigestConfigRecord,
  SmtpConfigRecord,
} from "@/components/alerts/types";

/* -------------------------------------------------------------------------- */
/* 常量与辅助                                                                   */
/* -------------------------------------------------------------------------- */

const CHANNEL_META: Record<string, { label: string; icon: string; desc: string; ready: boolean }> = {
  EMAIL: { label: "邮件 SMTP", icon: "✉", desc: "通过 SMTP 服务器发送预警邮件、摘要与周报", ready: true },
  WEBHOOK: { label: "Webhook", icon: "◎", desc: "自定义 HTTP 回调通知", ready: false },
  TELEGRAM: { label: "Telegram", icon: "➤", desc: "Telegram Bot 消息推送", ready: false },
};

function channelMeta(t: string) {
  return CHANNEL_META[t] ?? { label: t, icon: "◇", desc: "通知渠道", ready: true };
}

/* -------------------------------------------------------------------------- */
/* 页面                                                                        */
/* -------------------------------------------------------------------------- */

export default function AlertChannelsPage() {
  const smtpQ = useApi<SmtpConfigRecord>(
    () => asAlert<SmtpConfigRecord>(apiClient.alerts.getSmtpConfig()),
    [],
  );
  const digestQ = useApi<DigestConfigRecord>(
    () => asAlert<DigestConfigRecord>(apiClient.alerts.getDigestConfig()),
    [],
  );
  const channelsQ = useApi<ChannelListPayload>(
    () => asAlert<ChannelListPayload>(apiClient.alerts.getChannels()),
    [],
  );

  /* ------------------------------ 渠道测试 ------------------------------ */
  const [channelTestingId, setChannelTestingId] = useState<string | null>(null);
  const [channelBanner, setChannelBanner] = useState<BannerTone | null>(null);

  const handleChannelTest = async (ch: ChannelRecord) => {
    setChannelTestingId(ch.id);
    setChannelBanner(null);
    try {
      const res = await apiClient.alerts.testChannel(ch.id);
      if (res.success && res.data) {
        const r = res.data as { success?: boolean; recipient?: string; error?: string };
        setChannelBanner(
          r.success
            ? { tone: "success", text: `「${ch.channel_name}」测试消息已发送至 ${r.recipient ?? "收件人"}` }
            : { tone: "error", text: `「${ch.channel_name}」测试失败：${r.error ?? "未知错误"}` },
        );
      } else {
        setChannelBanner({ tone: "error", text: `「${ch.channel_name}」测试失败，请稍后重试` });
      }
    } catch (e) {
      setChannelBanner({
        tone: "error",
        text: e instanceof Error ? e.message : `「${ch.channel_name}」测试失败`,
      });
    } finally {
      setChannelTestingId(null);
    }
  };

  const savedItems = channelsQ.data?.items ?? [];
  const available = channelsQ.data?.available ?? [];

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        eyebrow="Notification Channels"
        title="通知渠道与摘要"
        description="配置 SMTP 邮件服务器、每日摘要与周报的定时推送，并管理通知渠道。"
      />

      {/* ============================ SMTP 配置 ============================ */}
      {smtpQ.error ? (
        <ErrorState error={smtpQ.error} lastUpdatedAt={smtpQ.lastUpdatedAt} onRetry={smtpQ.refresh} />
      ) : smtpQ.loading && !smtpQ.data ? (
        <section className={`${cardCls} p-4`}>
          <LoadingSkeleton variant="lines" rows={7} />
        </section>
      ) : smtpQ.data ? (
        <SmtpSection cfg={smtpQ.data} onSaved={smtpQ.refresh} />
      ) : (
        <section className={`${cardCls} p-4`}>
          <p className="text-xs text-muted">暂无 SMTP 配置。</p>
        </section>
      )}

      {/* ========================= 每日摘要 / 周报 ========================= */}
      {digestQ.error ? (
        <ErrorState error={digestQ.error} lastUpdatedAt={digestQ.lastUpdatedAt} onRetry={digestQ.refresh} />
      ) : digestQ.loading && !digestQ.data ? (
        <div className="grid gap-5 lg:grid-cols-2">
          <section className={`${cardCls} p-4`}>
            <LoadingSkeleton variant="lines" rows={5} />
          </section>
          <section className={`${cardCls} p-4`}>
            <LoadingSkeleton variant="lines" rows={5} />
          </section>
        </div>
      ) : digestQ.data ? (
        <DigestSections cfg={digestQ.data} onSaved={digestQ.refresh} />
      ) : null}

      {/* ============================ 渠道列表 ============================ */}
      <section className={`${cardCls} animate-fade-up overflow-hidden`} style={{ animationDelay: "180ms" }}>
        <div className="border-b border-line px-4 py-3">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-[#e6edf3]">
            <span aria-hidden>Channels</span> 渠道列表
          </h2>
          <p className="mt-0.5 text-[11px] text-muted">
            已接入的通知渠道类型与已保存的渠道配置。
          </p>
        </div>
        <div className="p-4">
          {channelsQ.error && (
            <ErrorState error={channelsQ.error} lastUpdatedAt={channelsQ.lastUpdatedAt} onRetry={channelsQ.refresh} />
          )}
          {channelsQ.loading && !channelsQ.data ? (
            <LoadingSkeleton variant="lines" rows={4} />
          ) : (
            <>
              {/* 可用渠道类型 */}
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {available.map((t) => {
                  const meta = channelMeta(t);
                  const count = savedItems.filter((c) => c.channel_type === t).length;
                  return (
                    <div
                      key={t}
                      className={`rounded-lg border border-line bg-bg px-3 py-2.5 ${
                        meta.ready ? "" : "opacity-60"
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <p className="flex items-center gap-1.5 text-xs font-medium text-[#e6edf3]">
                          <span aria-hidden>{meta.icon}</span>
                          {meta.label}
                        </p>
                        <span
                          className={`rounded border px-1.5 py-0.5 text-[10px] ${
                            meta.ready
                              ? "border-up/40 bg-up/10 text-up"
                              : "border-line text-muted"
                          }`}
                        >
                          {meta.ready ? (count > 0 ? `已配置 ${count} 条` : "未配置") : "即将支持"}
                        </span>
                      </div>
                      <p className="mt-1 text-[11px] leading-relaxed text-muted">{meta.desc}</p>
                    </div>
                  );
                })}
                {available.length === 0 && (
                  <p className="text-xs text-muted">暂无可用渠道类型。</p>
                )}
              </div>

              {/* 已保存配置 */}
              {channelBanner && (
                <Banner className="mt-3" tone={channelBanner.tone}>
                  {channelBanner.text}
                </Banner>
              )}
              {savedItems.length > 0 && (
                <ul className="mt-3 divide-y divide-line rounded-lg border border-line">
                  {savedItems.map((ch) => {
                    const configRecipient =
                      ch.config && typeof ch.config === "object" && "recipient" in ch.config
                        ? String((ch.config as Record<string, unknown>).recipient ?? "")
                        : "";
                    return (
                      <li key={ch.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2.5">
                        <span className="text-xs font-medium text-[#e6edf3]">{ch.channel_name}</span>
                        <span className="rounded border border-line px-1.5 py-0.5 font-mono text-[10px] uppercase text-muted">
                          {ch.channel_type}
                        </span>
                        {ch.is_primary && (
                          <span className="rounded border border-accent/40 bg-accent/10 px-1.5 py-0.5 text-[10px] text-accent">
                            主渠道
                          </span>
                        )}
                        {ch.is_enabled === false && (
                          <span className="rounded border border-muted/40 bg-muted/10 px-1.5 py-0.5 text-[10px] text-muted">
                            已停用
                          </span>
                        )}
                        {configRecipient && (
                          <span className="font-mono text-[11px] text-muted">{configRecipient}</span>
                        )}
                        <span className="ml-auto text-[11px] text-muted">
                          {formatDateTime(ch.updated_at ?? ch.created_at)}
                        </span>
                        <button
                          type="button"
                          className="rounded border border-accent/40 bg-accent/10 px-2 py-1 text-[11px] text-accent transition-colors hover:bg-accent/20 disabled:opacity-50"
                          onClick={() => void handleChannelTest(ch)}
                          disabled={channelTestingId !== null}
                        >
                          {channelTestingId === ch.id ? "测试中…" : "测试"}
                        </button>
                      </li>
                    );
                  })}
                </ul>
              )}
            </>
          )}
        </div>
      </section>
    </div>
  );
}
