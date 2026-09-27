"use client";

/**
 * 通知渠道表单区（供 /alerts/channels 使用）：
 *
 * - SmtpSection：SMTP 邮件配置卡（保存 + 发送测试邮件）
 * - DigestSections：每日摘要 + 周报双卡（共享收件人与启用开关）
 *
 * 两个 Section 均以 props.cfg 惰性初始化一次受控表单，
 * 数据到达时由父组件挂载（避免在 effect 中同步 setState）；
 * 挂载后轮询刷新不会覆盖未保存的编辑。
 */

import { useState } from "react";

import { apiClient } from "@/lib/api";

import { formatDateTime } from "./format";
import { Banner, Field, btnPrimary, cardCls, inputCls, selectCls } from "./ui";
import type { ChannelTestResult, DigestConfigRecord, SmtpConfigRecord } from "./types";

/* -------------------------------------------------------------------------- */
/* 常量与辅助                                                                   */
/* -------------------------------------------------------------------------- */

const HOUR_OPTIONS = Array.from({ length: 24 }, (_, h) => ({
  value: h,
  label: `${String(h).padStart(2, "0")}:00`,
}));

type Encryption = "NONE" | "STARTTLS" | "SSL";

const ENCRYPTION_OPTIONS: { value: Encryption; label: string }[] = [
  { value: "NONE", label: "无加密" },
  { value: "STARTTLS", label: "STARTTLS（常用）" },
  { value: "SSL", label: "SSL / TLS（465）" },
];

/** 内联提示条载荷（渠道页的渠道测试 banner 也使用该类型） */
export type BannerTone = { tone: "success" | "error"; text: string };

function encryptionOf(cfg: SmtpConfigRecord): Encryption {
  if (cfg.use_ssl) return "SSL";
  if (cfg.use_tls) return "STARTTLS";
  return "NONE";
}

function parseRecipients(text: string): string[] {
  return text
    .split(/[,;\n]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

interface SendOutcome {
  recipient?: string;
  success: boolean;
  message_id?: string | null;
  error?: string | null;
}

/** 解析 POST /digest/send 响应（{ sent, results } / { sent: false, reason }） */
function describeSendResult(data: Record<string, unknown> | null): BannerTone {
  if (data?.sent === true) {
    const results = Array.isArray(data.results) ? (data.results as SendOutcome[]) : [];
    const okCount = results.filter((r) => r.success).length;
    const total = results.length || 1;
    return { tone: "success", text: `发送完成：${okCount}/${total} 位收件人成功接收。` };
  }
  const reason = typeof data?.reason === "string" ? data.reason : "发送失败，请检查 SMTP 配置与收件人";
  return { tone: "error", text: reason };
}

/** 开关 */
function Toggle({
  checked,
  onChange,
  label,
  hint,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  hint?: string;
}) {
  return (
    <div className="flex items-center gap-2.5">
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        onClick={() => onChange(!checked)}
        className={`relative h-5 w-9 shrink-0 rounded-full transition-colors ${
          checked ? "bg-accent/80" : "bg-line"
        }`}
      >
        <span
          aria-hidden
          className={`absolute top-0.5 h-4 w-4 rounded-full bg-[#e6edf3] shadow transition-all ${
            checked ? "left-[18px]" : "left-0.5"
          }`}
        />
      </button>
      <div className="min-w-0">
        <p className="text-xs font-medium text-[#e6edf3]">{label}</p>
        {hint && <p className="text-[11px] leading-relaxed text-muted">{hint}</p>}
      </div>
    </div>
  );
}

/** SMTP 测试结果展示块 */
function SmtpTestResult({ result }: { result: ChannelTestResult }) {
  const ok = result.success;
  return (
    <div
      role={ok ? "status" : "alert"}
      className={`animate-fade-up rounded-lg border px-3 py-2.5 text-xs leading-relaxed ${
        ok ? "border-up/40 bg-up/10 text-up" : "border-down/40 bg-down/10 text-down"
      }`}
    >
      <p className="font-semibold">
        {ok ? "✓ 测试邮件已发送" : "✕ 测试失败"}
        {result.recipient && <span className="ml-1 font-normal opacity-80">→ {result.recipient}</span>}
      </p>
      {ok && result.message_id && (
        <p className="mt-1 break-all font-mono text-[10px] opacity-80">message_id: {result.message_id}</p>
      )}
      {!ok && result.error && <p className="mt-1">错误原因：{result.error}</p>}
      {result.response !== undefined && result.response !== null && result.response !== "" && (
        <pre className="mt-1.5 max-h-32 overflow-auto whitespace-pre-wrap break-all rounded border border-current/20 bg-bg-deep/60 px-2 py-1.5 font-mono text-[10px] opacity-90">
          {typeof result.response === "string" ? result.response : JSON.stringify(result.response, null, 2)}
        </pre>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* SMTP 配置卡                                                                  */
/* -------------------------------------------------------------------------- */

interface SmtpFormState {
  host: string;
  port: string;
  encryption: Encryption;
  user: string;
  password: string;
  fromEmail: string;
  fromName: string;
  recipient: string;
}

export function SmtpSection({ cfg, onSaved }: { cfg: SmtpConfigRecord; onSaved: () => void }) {
  const [form, setForm] = useState<SmtpFormState>(() => ({
    host: cfg.host ?? "",
    port: cfg.port != null ? String(cfg.port) : "465",
    encryption: encryptionOf(cfg),
    user: cfg.user ?? "",
    password: "",
    fromEmail: cfg.from_email ?? "",
    fromName: cfg.from_name ?? "",
    recipient: cfg.recipient ?? "",
  }));
  const [hasPassword, setHasPassword] = useState(() => Boolean(cfg.password));
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [banner, setBanner] = useState<BannerTone | null>(null);
  const [testResult, setTestResult] = useState<ChannelTestResult | null>(null);

  const patch = (p: Partial<SmtpFormState>) => setForm((f) => ({ ...f, ...p }));

  const handleSave = async () => {
    if (!form.host.trim()) {
      setBanner({ tone: "error", text: "请填写 SMTP 服务器地址（如 smtp.qq.com）" });
      return;
    }
    const portNum = Number(form.port);
    if (!Number.isInteger(portNum) || portNum < 1 || portNum > 65535) {
      setBanner({ tone: "error", text: "端口需为 1-65535 的整数（SSL 常用 465，STARTTLS 常用 587）" });
      return;
    }
    setSaving(true);
    setBanner(null);
    try {
      const body: Record<string, unknown> = {
        host: form.host.trim(),
        port: portNum,
        user: form.user.trim(),
        from_email: form.fromEmail.trim(),
        from_name: form.fromName.trim(),
        recipient: form.recipient.trim(),
        use_tls: form.encryption === "STARTTLS",
        use_ssl: form.encryption === "SSL",
      };
      if (form.password) body.password = form.password;
      const res = await apiClient.alerts.updateSmtpConfig(body);
      if (res.success) {
        if (form.password) setHasPassword(true);
        patch({ password: "" });
        setBanner({ tone: "success", text: "SMTP 配置已保存" });
        onSaved();
      } else {
        setBanner({ tone: "error", text: "保存失败，请检查配置后重试" });
      }
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : "保存失败" });
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const res = await apiClient.alerts.testSmtp({});
      if (res.success && res.data) {
        setTestResult(res.data as unknown as ChannelTestResult);
      } else {
        setTestResult({ success: false, error: "测试请求失败，请稍后重试" });
      }
    } catch (e) {
      setTestResult({ success: false, error: e instanceof Error ? e.message : "测试请求失败" });
    } finally {
      setTesting(false);
    }
  };

  return (
    <section className={`${cardCls} animate-fade-up overflow-hidden`}>
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-4 py-3">
        <div>
          <h2 className="flex items-center gap-2 text-sm font-semibold text-[#e6edf3]">
            <span aria-hidden>✉</span> SMTP 邮件配置
          </h2>
          <p className="mt-0.5 text-[11px] text-muted">
            预警通知、摘要与周报均通过该邮箱服务发送。
          </p>
        </div>
        <button
          type="button"
          className={btnPrimary}
          onClick={() => void handleTest()}
          disabled={testing}
          title="使用已保存的配置向默认收件人发送一封测试邮件"
        >
          {testing ? "发送中…" : "发送测试邮件"}
        </button>
      </div>

      <div className="p-4">
        <div className="grid gap-x-4 gap-y-3.5 sm:grid-cols-2 lg:grid-cols-3">
          <Field label="SMTP 服务器" required>
            <input
              className={inputCls}
              value={form.host}
              onChange={(e) => patch({ host: e.target.value })}
              placeholder="smtp.qq.com"
            />
          </Field>
          <Field label="端口" required>
            <input
              className={inputCls}
              type="number"
              min={1}
              max={65535}
              value={form.port}
              onChange={(e) => patch({ port: e.target.value })}
              placeholder="465"
            />
          </Field>
          <Field label="加密方式">
            <select
              className={selectCls}
              value={form.encryption}
              onChange={(e) => patch({ encryption: e.target.value as Encryption })}
            >
              {ENCRYPTION_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="用户名">
            <input
              className={inputCls}
              value={form.user}
              onChange={(e) => patch({ user: e.target.value })}
              placeholder="通常为发件人邮箱"
              autoComplete="off"
            />
          </Field>
          <Field label="密码" hint="出于安全考虑已保存的密码不回显；留空表示保留原密码。">
            <input
              className={inputCls}
              type="password"
              value={form.password}
              onChange={(e) => patch({ password: e.target.value })}
              placeholder={hasPassword ? "••••••••（留空保留）" : "未设置"}
              autoComplete="new-password"
            />
          </Field>
          <Field label="发件人邮箱">
            <input
              className={inputCls}
              value={form.fromEmail}
              onChange={(e) => patch({ fromEmail: e.target.value })}
              placeholder="alerts@example.com"
            />
          </Field>
          <Field label="发件人名称">
            <input
              className={inputCls}
              value={form.fromName}
              onChange={(e) => patch({ fromName: e.target.value })}
              placeholder="BTC 数据分析平台"
            />
          </Field>
          <Field
            label="默认收件人"
            hint="摘要、周报与测试邮件在未单独指定收件人时使用该地址。"
            className="lg:col-span-2"
          >
            <input
              className={inputCls}
              value={form.recipient}
              onChange={(e) => patch({ recipient: e.target.value })}
              placeholder="you@example.com"
            />
          </Field>
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-3">
          <button type="button" className={btnPrimary} onClick={() => void handleSave()} disabled={saving}>
            {saving ? "保存中…" : "保存配置"}
          </button>
          <span className="text-[11px] text-muted">
            提示：修改后需先保存，测试邮件使用的是已保存配置。
          </span>
        </div>

        {banner && <Banner className="mt-3" tone={banner.tone}>{banner.text}</Banner>}
        {testResult && (
          <div className="mt-3">
            <SmtpTestResult result={testResult} />
          </div>
        )}
      </div>
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/* 每日摘要 + 周报                                                               */
/* -------------------------------------------------------------------------- */

interface DigestFormState {
  dailyEnabled: boolean;
  dailyHour: number;
  weeklyDay: number;
  weeklyHour: number;
  recipients: string;
}

const SEND_BTN_CLS =
  "inline-flex items-center justify-center gap-1.5 rounded-lg border border-line bg-bg px-3 py-1.5 text-xs text-[#e6edf3] transition-colors hover:border-accent/50 hover:text-accent disabled:cursor-not-allowed disabled:opacity-50";

export function DigestSections({ cfg, onSaved }: { cfg: DigestConfigRecord; onSaved: () => void }) {
  const [form, setForm] = useState<DigestFormState>(() => ({
    dailyEnabled: cfg.enabled ?? true,
    dailyHour: cfg.daily_hour ?? 8,
    weeklyDay: cfg.weekly_day ?? 0,
    weeklyHour: cfg.weekly_hour ?? 20,
    recipients: (cfg.recipients ?? []).join(", "),
  }));
  const [saving, setSaving] = useState<"daily" | "weekly" | null>(null);
  const [sendBusy, setSendBusy] = useState<"daily" | "weekly" | null>(null);
  const [dailyBanner, setDailyBanner] = useState<BannerTone | null>(null);
  const [weeklyBanner, setWeeklyBanner] = useState<BannerTone | null>(null);

  const patch = (p: Partial<DigestFormState>) => setForm((f) => ({ ...f, ...p }));

  const handleSave = async (part: "daily" | "weekly") => {
    setSaving(part);
    const clear = part === "daily" ? setDailyBanner : setWeeklyBanner;
    clear(null);
    try {
      const recipients = parseRecipients(form.recipients);
      const body =
        part === "daily"
          ? { enabled: form.dailyEnabled, daily_hour: form.dailyHour, recipients }
          : { weekly_day: form.weeklyDay, weekly_hour: form.weeklyHour, recipients };
      const res = await apiClient.alerts.updateDigestConfig(body as Record<string, unknown>);
      if (res.success) {
        clear({ tone: "success", text: "摘要配置已保存" });
        onSaved();
      } else {
        clear({ tone: "error", text: "保存失败，请稍后重试" });
      }
    } catch (e) {
      clear({ tone: "error", text: e instanceof Error ? e.message : "保存失败" });
    } finally {
      setSaving(null);
    }
  };

  const handleSend = async (type: "daily" | "weekly") => {
    setSendBusy(type);
    const setBanner = type === "daily" ? setDailyBanner : setWeeklyBanner;
    setBanner(null);
    try {
      const recipients = parseRecipients(form.recipients);
      const res = await apiClient.alerts.sendDigest({
        type,
        recipients: recipients.length ? recipients : null,
      });
      setBanner(describeSendResult(res.success ? (res.data as Record<string, unknown>) : null));
      onSaved();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : "发送失败" });
    } finally {
      setSendBusy(null);
    }
  };

  return (
    <div className="grid gap-5 lg:grid-cols-2">
      {/* 每日摘要 */}
      <section className={`${cardCls} animate-fade-up overflow-hidden`} style={{ animationDelay: "60ms" }}>
        <div className="border-b border-line px-4 py-3">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-[#e6edf3]">
            <span aria-hidden>☀</span> 每日摘要
          </h2>
          <p className="mt-0.5 text-[11px] text-muted">
            每天定时推送：当前价格、24h 涨跌、指标概览与今日触发统计。
          </p>
        </div>
        <div className="flex flex-col gap-4 p-4">
          <Toggle
            checked={form.dailyEnabled}
            onChange={(v) => patch({ dailyEnabled: v })}
            label="启用定时摘要（含周报）"
            hint="关闭后将停止所有定时邮件推送，手动发送不受影响。"
          />
          <Field label="发送时间">
            <select
              className={selectCls}
              value={form.dailyHour}
              onChange={(e) => patch({ dailyHour: Number(e.target.value) })}
            >
              {HOUR_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  每日 {o.label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="收件人" hint="多个邮箱用逗号或换行分隔；留空则使用 SMTP 默认收件人。">
            <textarea
              className={`${inputCls} min-h-[64px] resize-y`}
              value={form.recipients}
              onChange={(e) => patch({ recipients: e.target.value })}
              placeholder="留空则使用 SMTP 默认收件人"
            />
          </Field>
          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              className={btnPrimary}
              onClick={() => void handleSave("daily")}
              disabled={saving !== null}
            >
              {saving === "daily" ? "保存中…" : "保存"}
            </button>
            <button
              type="button"
              className={SEND_BTN_CLS}
              onClick={() => void handleSend("daily")}
              disabled={sendBusy !== null}
            >
              {sendBusy === "daily" ? "发送中…" : "立即发送"}
            </button>
            <span className="text-[11px] text-muted">
              上次发送：{formatDateTime(cfg.last_daily_sent)}
            </span>
          </div>
          {dailyBanner && <Banner tone={dailyBanner.tone}>{dailyBanner.text}</Banner>}
        </div>
      </section>

      {/* 周报 */}
      <section className={`${cardCls} animate-fade-up overflow-hidden`} style={{ animationDelay: "120ms" }}>
        <div className="border-b border-line px-4 py-3">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-[#e6edf3]">
            <span aria-hidden>📅</span> 周报
          </h2>
          <p className="mt-0.5 text-[11px] text-muted">
            每周定时推送：周内涨跌、高低点、指标均值与触发事件回顾。
          </p>
        </div>
        <div className="flex flex-col gap-4 p-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="发送星期">
              <select
                className={selectCls}
                value={form.weeklyDay}
                onChange={(e) => patch({ weeklyDay: Number(e.target.value) })}
              >
                {WEEKDAY_LIST.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="发送时间">
              <select
                className={selectCls}
                value={form.weeklyHour}
                onChange={(e) => patch({ weeklyHour: Number(e.target.value) })}
              >
                {HOUR_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <p className="text-[11px] leading-relaxed text-muted">
            周报与每日摘要共用上方收件人列表与启用开关。
          </p>
          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              className={btnPrimary}
              onClick={() => void handleSave("weekly")}
              disabled={saving !== null}
            >
              {saving === "weekly" ? "保存中…" : "保存"}
            </button>
            <button
              type="button"
              className={SEND_BTN_CLS}
              onClick={() => void handleSend("weekly")}
              disabled={sendBusy !== null}
            >
              {sendBusy === "weekly" ? "发送中…" : "立即发送"}
            </button>
            <span className="text-[11px] text-muted">
              上次发送：{formatDateTime(cfg.last_weekly_sent)}
            </span>
          </div>
          {weeklyBanner && <Banner tone={weeklyBanner.tone}>{weeklyBanner.text}</Banner>}
        </div>
      </section>
    </div>
  );
}

/** 周报星期（weekly_day 0-6，0 = 周日） */
const WEEKDAY_LIST: { value: number; label: string }[] = [
  { value: 0, label: "周日" },
  { value: 1, label: "周一" },
  { value: 2, label: "周二" },
  { value: 3, label: "周三" },
  { value: 4, label: "周四" },
  { value: 5, label: "周五" },
  { value: 6, label: "周六" },
];
