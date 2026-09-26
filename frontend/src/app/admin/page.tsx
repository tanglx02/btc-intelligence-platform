"use client";

/**
 * 后台管理（Admin）。
 *
 * - 系统健康概览：DB / Redis / Provider 在线数 / 任务运行数（GET /system/health + /system/jobs）
 * - 数据库统计：各核心表行数与最近数据时间（GET /system/stats）
 * - SMTP 邮件配置表单：服务器/端口/加密方式/用户名/密码（掩码）/发件人/收件人，
 *   保存（PUT /alerts/smtp/config）与发送测试邮件（POST /alerts/smtp/test）
 * - 告警摘要调度配置（GET/PUT /alerts/digest/config）
 *
 * 原则：绝不伪造状态 —— 写操作失败时行内展示错误原因；密码仅显示掩码。
 */

import { useEffect, useMemo, useRef, useState } from "react";

import { apiClient } from "@/lib/api";
import { useApi, usePolling } from "@/hooks/useApi";
import { ErrorState } from "@/components/common/ErrorState";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";

import { systemApi } from "@/components/system/api";
import {
  DataTable,
  Field,
  InlineNotice,
  PageHeader,
  SectionCard,
  StatChip,
  StatusDot,
  btnGhost,
  btnPrimary,
  btnSuccess,
  formatDateTime,
  formatRelative,
  fmtInt,
  inputClass,
  tdClass,
  tdMutedClass,
} from "@/components/system/ui";
import type {
  DigestConfigData,
  JobsPayload,
  SmtpConfigData,
  SmtpTestResult,
  SystemHealthData,
  SystemStatsData,
} from "@/components/system/types";

import type { ApiResponse } from "@/types/api";

const HEALTH_POLL_MS = 15_000;
const JOBS_POLL_MS = 15_000;

type Encryption = "none" | "starttls" | "ssl";
type Notice = { tone: "success" | "error" | "info"; text: string } | null;

export default function AdminPage() {
  const healthRes = usePolling<SystemHealthData>(
    () => apiClient.system.getHealth() as unknown as Promise<ApiResponse<SystemHealthData>>,
    HEALTH_POLL_MS,
  );
  const jobsRes = usePolling<JobsPayload>(
    () => apiClient.system.getJobs() as unknown as Promise<ApiResponse<JobsPayload>>,
    JOBS_POLL_MS,
  );
  const statsRes = useApi<SystemStatsData>(
    () => apiClient.system.getStats() as unknown as Promise<ApiResponse<SystemStatsData>>,
  );

  const health = healthRes.data;
  const jobsCount = useMemo(() => {
    const data = jobsRes.data as unknown;
    if (data && typeof data === "object" && Array.isArray((data as JobsPayload).jobs)) {
      return (data as JobsPayload).jobs ?? [];
    }
    return [];
  }, [jobsRes.data]);
  const runningJobs = jobsCount.filter((j) => (j.status ?? "").toUpperCase() === "RUNNING").length;

  const providers = health?.providers ?? null;
  const onlineProviders = providers?.by_status?.ONLINE ?? 0;
  const degradedProviders =
    (providers?.by_status?.DEGRADED ?? 0) +
    (providers?.by_status?.SLOW ?? 0) +
    (providers?.by_status?.RATE_LIMITED ?? 0);

  return (
    <div className="space-y-5">
      <PageHeader
        kicker="Admin · Console"
        title="后台管理"
        description="系统健康、数据库统计与告警通知配置 —— 管理中枢。本页写操作直接影响系统运行，请谨慎操作。"
      >
        <span className="rounded-lg border border-line bg-bg px-2.5 py-1 text-[11px] text-muted">
          健康轮询 15s · 更新 {formatRelative(healthRes.lastUpdatedAt)}
        </span>
      </PageHeader>

      {/* 1. 系统健康概览 */}
      <section className="animate-fade-up space-y-3" style={{ animationDelay: "60ms" }}>
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-xs font-semibold text-muted">整体状态</span>
          {healthRes.loading && !health ? (
            <LoadingSkeleton variant="lines" rows={1} className="w-24" />
          ) : (
            <StatusDot status={health?.status ?? null} className="text-sm" />
          )}
          {health?.time && (
            <span className="text-[11px] text-muted">检查时间 {formatDateTime(health.time)}</span>
          )}
        </div>
        {healthRes.error && !health ? (
          <ErrorState error={healthRes.error} lastUpdatedAt={healthRes.lastUpdatedAt} onRetry={healthRes.refresh} />
        ) : healthRes.loading && !health ? (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {Array.from({ length: 4 }, (_, i) => (
              <LoadingSkeleton key={i} variant="card" />
            ))}
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <HealthCard
              title="数据库"
              ok={health?.database?.ok === true}
              detail={
                health?.database?.ok
                  ? `SELECT 1 · ${health.database.latency_ms ?? "—"} ms`
                  : health?.database?.error ?? "连接失败"
              }
            />
            <HealthCard
              title="Redis"
              ok={health?.redis?.ok === true}
              detail={
                health?.redis?.ok
                  ? `PING · ${health.redis.latency_ms ?? "—"} ms`
                  : health?.redis?.error ?? "连接失败（缓存层可降级）"
              }
            />
            <HealthCard
              title="Provider 在线"
              ok={(onlineProviders ?? 0) > 0}
              detail={`在线 ${onlineProviders ?? 0} / 共 ${providers?.total ?? 0} · 降级类 ${degradedProviders ?? 0}`}
            />
            <HealthCard
              title="任务运行中"
              ok={runningJobs > 0}
              detail={`运行中 ${runningJobs} / 注册 ${jobsCount.length}`}
            />
          </div>
        )}
      </section>

      {/* 2. 数据库统计 */}
      <StatsSection
        stats={statsRes.data}
        loading={statsRes.loading && !statsRes.data}
        error={statsRes.error}
        lastUpdatedAt={statsRes.lastUpdatedAt}
        onRetry={statsRes.refresh}
      />

      {/* 3. SMTP 邮件配置 */}
      <SmtpSection />

      {/* 4. 告警摘要调度配置 */}
      <DigestSection />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 健康卡片                                                                    */
/* -------------------------------------------------------------------------- */

function HealthCard({ title, ok, detail }: { title: string; ok: boolean; detail: string }) {
  return (
    <div
      className={`rounded-xl border bg-bg-raised p-4 transition-colors ${
        ok ? "border-line hover:border-up/40" : "border-down/40"
      }`}
    >
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold text-[#e6edf3]">{title}</h3>
        <StatusDot status={ok ? "ok" : "critical"} />
      </div>
      <p className="mt-2 break-all font-mono text-[11px] leading-relaxed text-muted">{detail}</p>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 数据库统计                                                                  */
/* -------------------------------------------------------------------------- */

const TABLE_LABELS: Record<string, string> = {
  candles: "K 线（candles）",
  market_prices: "实时价格（market_prices）",
  onchain_metrics: "链上指标（onchain_metrics）",
  etf_flows: "ETF 资金流（etf_flows）",
  derivatives: "衍生品（derivatives）",
  macro_series: "宏观序列（macro_series）",
  sentiment: "情绪（sentiment）",
  indicator_values: "技术指标值（indicator_values）",
};

function StatsSection({
  stats,
  loading,
  error,
  lastUpdatedAt,
  onRetry,
}: {
  stats: SystemStatsData | null;
  loading: boolean;
  error: ReturnType<typeof useApi>["error"];
  lastUpdatedAt: number | null;
  onRetry: () => void;
}) {
  const tables = stats?.tables ?? {};
  const rows = Object.entries(tables);
  return (
    <SectionCard
      title="数据库统计"
      subtitle="各核心数据表行数与最近数据时间（单表查询失败不影响其余）"
      right={
        <div className="flex items-center gap-2">
          {typeof stats?.provider_count === "number" && (
            <StatChip label="Provider" value={stats.provider_count} />
          )}
          <span className="text-[11px] text-muted">更新 {formatRelative(lastUpdatedAt)}</span>
        </div>
      }
    >
      {error && !stats ? (
        <ErrorState error={error} lastUpdatedAt={lastUpdatedAt} onRetry={onRetry} />
      ) : loading ? (
        <LoadingSkeleton variant="table" rows={5} />
      ) : rows.length === 0 ? (
        <p className="py-6 text-center text-xs text-muted">暂无统计（统计端点未返回表数据）。</p>
      ) : (
        <DataTable columns={["数据表", "行数", "最近数据时间", "相对时间"]} minWidth={620}>
          {rows.map(([name, entry]) => (
            <tr key={name} className="hover:bg-bg-hover">
              <td className={tdClass}>{TABLE_LABELS[name] ?? name}</td>
              <td className={tdMutedClass}>{entry?.error ? "查询失败" : fmtInt(entry?.rows)}</td>
              <td className={tdMutedClass}>{formatDateTime(entry?.latest_observation_time)}</td>
              <td className={tdMutedClass}>{formatRelative(entry?.latest_observation_time)}</td>
            </tr>
          ))}
        </DataTable>
      )}
    </SectionCard>
  );
}

/* -------------------------------------------------------------------------- */
/* SMTP 邮件配置                                                               */
/* -------------------------------------------------------------------------- */

const ENCRYPTION_OPTIONS: { value: Encryption; label: string }[] = [
  { value: "none", label: "无（明文，不推荐）" },
  { value: "starttls", label: "STARTTLS（推荐）" },
  { value: "ssl", label: "SSL/TLS" },
];

function SmtpSection() {
  const smtpRes = useApi<SmtpConfigData>(() => systemApi.getSmtpConfig());
  const config = smtpRes.data;
  const passwordSet = config?.password === "***";

  const [form, setForm] = useState({
    enabled: true,
    host: "",
    port: "",
    encryption: "starttls" as Encryption,
    user: "",
    password: "",
    fromEmail: "",
    fromName: "",
    recipient: "",
  });
  const hydratedRef = useRef(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [testResult, setTestResult] = useState<SmtpTestResult | null>(null);
  const [testError, setTestError] = useState<string | null>(null);

  /* 首次加载后水合表单（后续刷新不覆盖用户编辑） */
  useEffect(() => {
    if (!config || hydratedRef.current) return;
    hydratedRef.current = true;
    setForm({
      enabled: config.enabled !== false,
      host: config.host ?? "",
      port: config.port !== null && config.port !== undefined ? String(config.port) : "",
      encryption: config.use_ssl ? "ssl" : config.use_tls ? "starttls" : "none",
      user: config.user ?? "",
      password: "",
      fromEmail: config.from_email ?? "",
      fromName: config.from_name ?? "",
      recipient: config.recipient ?? "",
    });
  }, [config]);

  function patch<K extends keyof typeof form>(key: K, value: (typeof form)[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  async function save() {
    setSaving(true);
    setNotice(null);
    try {
      const port = form.port.trim();
      if (port) {
        const n = Number(port);
        if (!Number.isInteger(n) || n < 1 || n > 65535) {
          throw new Error("端口需为 1-65535 的整数");
        }
      }
      const body: Record<string, unknown> = {
        enabled: form.enabled,
        use_tls: form.encryption === "starttls",
        use_ssl: form.encryption === "ssl",
      };
      if (form.host.trim()) body.host = form.host.trim();
      if (port) body.port = Number(port);
      if (form.user.trim()) body.user = form.user.trim();
      if (form.password) body.password = form.password; // 留空 = 保留旧值（后端约定）
      if (form.fromEmail.trim()) body.from_email = form.fromEmail.trim();
      if (form.fromName.trim()) body.from_name = form.fromName.trim();
      if (form.recipient.trim()) body.recipient = form.recipient.trim();

      const res = await systemApi.updateSmtpConfig(body);
      if (res.success === false) throw new Error(res.error?.message ?? "保存失败");
      setNotice({ tone: "success", text: "SMTP 配置已保存；密码留空时保留原值。" });
    } catch (err) {
      setNotice({ tone: "error", text: err instanceof Error ? err.message : "保存失败" });
    } finally {
      setSaving(false);
    }
  }

  async function sendTest() {
    setTesting(true);
    setTestResult(null);
    setTestError(null);
    try {
      const res = await systemApi.testSmtp(form.recipient.trim() || undefined);
      if (res.success === false) {
        setTestError(res.error?.message ?? "测试请求失败");
      } else {
        const data = res.data;
        if (data?.success === false) {
          setTestError(data.error ?? "发送失败（未返回原因）");
        }
        setTestResult(data ?? null);
      }
    } catch (err) {
      setTestError(err instanceof Error ? err.message : "测试请求失败");
    } finally {
      setTesting(false);
    }
  }

  return (
    <SectionCard
      title="SMTP 邮件配置"
      subtitle="告警通知与摘要邮件的发信通道 · 密码保存后仅显示掩码"
      right={
        <span className="text-[11px] text-muted">
          {smtpRes.loading && !config ? "加载中…" : `更新 ${formatRelative(smtpRes.lastUpdatedAt)}`}
        </span>
      }
    >
      {smtpRes.error && !config ? (
        <ErrorState error={smtpRes.error} lastUpdatedAt={smtpRes.lastUpdatedAt} onRetry={smtpRes.refresh} />
      ) : (
        <div className="space-y-4">
          {/* 密码掩码提示 */}
          <div className="flex flex-wrap items-center gap-2">
            <StatChip
              label="通道状态"
              value={config?.enabled === false ? "已禁用" : "已启用"}
              tone={config?.enabled === false ? "warn" : "up"}
            />
            <StatChip label="密码" value={passwordSet ? "已配置（掩码）" : "未配置"} tone={passwordSet ? "up" : "warn"} />
          </div>

          <div className="grid grid-cols-1 gap-x-5 gap-y-4 sm:grid-cols-2 lg:grid-cols-3">
            <Field label="SMTP 服务器" hint="如 smtp.example.com">
              <input
                className={inputClass}
                value={form.host}
                onChange={(e) => patch("host", e.target.value)}
                placeholder="smtp.example.com"
                autoComplete="off"
              />
            </Field>
            <Field label="端口" hint="1-65535">
              <input
                className={inputClass}
                value={form.port}
                onChange={(e) => patch("port", e.target.value)}
                placeholder="465 / 587"
                inputMode="numeric"
                autoComplete="off"
              />
            </Field>
            <Field label="加密方式">
              <select
                className={inputClass}
                value={form.encryption}
                onChange={(e) => patch("encryption", e.target.value as Encryption)}
              >
                {ENCRYPTION_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value} className="bg-bg-raised">
                    {o.label}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="用户名">
              <input
                className={inputClass}
                value={form.user}
                onChange={(e) => patch("user", e.target.value)}
                placeholder="user@example.com"
                autoComplete="off"
              />
            </Field>
            <Field label="密码" hint={passwordSet ? "已配置，留空保留原值" : "未配置"}>
              <input
                className={inputClass}
                type="password"
                value={form.password}
                onChange={(e) => patch("password", e.target.value)}
                placeholder={passwordSet ? "••••••••（留空保留）" : "输入密码"}
                autoComplete="new-password"
              />
            </Field>
            <Field label="发件人邮箱">
              <input
                className={inputClass}
                value={form.fromEmail}
                onChange={(e) => patch("fromEmail", e.target.value)}
                placeholder="alert@example.com"
                autoComplete="off"
              />
            </Field>
            <Field label="发件人名称">
              <input
                className={inputClass}
                value={form.fromName}
                onChange={(e) => patch("fromName", e.target.value)}
                placeholder="BTC 研究平台"
                autoComplete="off"
              />
            </Field>
            <Field label="收件人邮箱" hint="默认告警收件人">
              <input
                className={inputClass}
                value={form.recipient}
                onChange={(e) => patch("recipient", e.target.value)}
                placeholder="you@example.com"
                autoComplete="off"
              />
            </Field>
            <Field label="通道开关">
              <label className="flex h-[38px] cursor-pointer items-center gap-2 rounded-lg border border-line bg-bg px-3">
                <input
                  type="checkbox"
                  className="h-3.5 w-3.5 accent-[#58a6ff]"
                  checked={form.enabled}
                  onChange={(e) => patch("enabled", e.target.checked)}
                />
                <span className="text-xs text-[#e6edf3]">启用 SMTP 通知</span>
              </label>
            </Field>
          </div>

          {notice && <InlineNotice tone={notice.tone}>{notice.text}</InlineNotice>}

          {/* 测试结果 */}
          {testing && <LoadingSkeleton variant="lines" rows={2} />}
          {testError && <InlineNotice tone="error">测试邮件发送失败：{testError}</InlineNotice>}
          {testResult && !testError && (
            <div className="rounded-lg border border-up/40 bg-up/[0.07] px-3 py-2.5 text-xs">
              <p className="font-medium text-up">
                测试邮件已发送至 {testResult.recipient ?? (form.recipient || "（后端默认收件人）")}
              </p>
              {testResult.message_id && (
                <p className="mt-1 break-all font-mono text-[11px] text-muted">
                  Message-ID: {testResult.message_id}
                </p>
              )}
            </div>
          )}

          <div className="flex flex-wrap items-center gap-2 border-t border-line/60 pt-4">
            <button type="button" onClick={() => void save()} disabled={saving} className={btnPrimary}>
              {saving ? "保存中…" : "保存配置"}
            </button>
            <button
              type="button"
              onClick={() => void sendTest()}
              disabled={testing}
              className={btnSuccess}
              title="使用已保存配置发送测试邮件"
            >
              {testing ? "发送中…" : "发送测试邮件"}
            </button>
            <button
              type="button"
              onClick={() => {
                smtpRes.refresh();
                setNotice({ tone: "info", text: "已从后端重新读取配置（表单编辑将被覆盖）。" });
              }}
              className={btnGhost}
            >
              重新加载
            </button>
          </div>
        </div>
      )}
    </SectionCard>
  );
}

/* -------------------------------------------------------------------------- */
/* 告警摘要调度配置                                                             */
/* -------------------------------------------------------------------------- */

const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

function DigestSection() {
  const digestRes = useApi<DigestConfigData>(() => systemApi.getDigestConfig());
  const config = digestRes.data;

  const [form, setForm] = useState({
    enabled: false,
    dailyHour: "8",
    weeklyDay: "1",
    weeklyHour: "8",
    recipients: "",
  });
  const hydratedRef = useRef(false);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);

  useEffect(() => {
    if (!config || hydratedRef.current) return;
    hydratedRef.current = true;
    setForm({
      enabled: config.enabled === true,
      dailyHour: String(config.daily_hour ?? 8),
      weeklyDay: String(config.weekly_day ?? 1),
      weeklyHour: String(config.weekly_hour ?? 8),
      recipients: Array.isArray(config.recipients) ? config.recipients.join("\n") : "",
    });
  }, [config]);

  async function save() {
    setSaving(true);
    setNotice(null);
    try {
      const dailyHour = clampHour(form.dailyHour);
      const weeklyHour = clampHour(form.weeklyHour);
      const weeklyDay = Number(form.weeklyDay);
      if (!Number.isInteger(weeklyDay) || weeklyDay < 0 || weeklyDay > 6) {
        throw new Error("每周发送日需为 0-6（周日-周六）");
      }
      const recipients = form.recipients
        .split(/[\n,;，；]/)
        .map((s) => s.trim())
        .filter(Boolean);

      const res = await systemApi.updateDigestConfig({
        enabled: form.enabled,
        daily_hour: dailyHour,
        weekly_day: weeklyDay,
        weekly_hour: weeklyHour,
        recipients,
      });
      if (res.success === false) throw new Error(res.error?.message ?? "保存失败");
      setNotice({ tone: "success", text: "告警摘要调度配置已保存。" });
      digestRes.refresh();
    } catch (err) {
      setNotice({ tone: "error", text: err instanceof Error ? err.message : "保存失败" });
    } finally {
      setSaving(false);
    }
  }

  return (
    <SectionCard
      title="告警摘要与通知调度"
      subtitle="预警事件的每日 / 每周邮件摘要调度；扫描间隔等引擎参数当前由后端配置管理"
      right={
        <span className="text-[11px] text-muted">
          {digestRes.loading && !config ? "加载中…" : `更新 ${formatRelative(digestRes.lastUpdatedAt)}`}
        </span>
      }
    >
      {digestRes.error && !config ? (
        <ErrorState error={digestRes.error} lastUpdatedAt={digestRes.lastUpdatedAt} onRetry={digestRes.refresh} />
      ) : (
        <div className="space-y-4">
          <div className="grid grid-cols-1 gap-x-5 gap-y-4 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="摘要邮件">
              <label className="flex h-[38px] cursor-pointer items-center gap-2 rounded-lg border border-line bg-bg px-3">
                <input
                  type="checkbox"
                  className="h-3.5 w-3.5 accent-[#58a6ff]"
                  checked={form.enabled}
                  onChange={(e) => setForm((p) => ({ ...p, enabled: e.target.checked }))}
                />
                <span className="text-xs text-[#e6edf3]">启用摘要推送</span>
              </label>
            </Field>
            <Field label="每日摘要发送时刻" hint="0-23 点">
              <input
                className={inputClass}
                value={form.dailyHour}
                onChange={(e) => setForm((p) => ({ ...p, dailyHour: e.target.value }))}
                inputMode="numeric"
              />
            </Field>
            <Field label="每周摘要发送日">
              <select
                className={inputClass}
                value={form.weeklyDay}
                onChange={(e) => setForm((p) => ({ ...p, weeklyDay: e.target.value }))}
              >
                {WEEKDAYS.map((d, i) => (
                  <option key={d} value={i} className="bg-bg-raised">
                    {d}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="每周摘要发送时刻" hint="0-23 点">
              <input
                className={inputClass}
                value={form.weeklyHour}
                onChange={(e) => setForm((p) => ({ ...p, weeklyHour: e.target.value }))}
                inputMode="numeric"
              />
            </Field>
          </div>
          <Field label="摘要收件人" hint="每行一个邮箱，逗号分隔亦可">
            <textarea
              className={`${inputClass} min-h-[72px] resize-y`}
              value={form.recipients}
              onChange={(e) => setForm((p) => ({ ...p, recipients: e.target.value }))}
              placeholder={"a@example.com\nb@example.com"}
            />
          </Field>

          {notice && <InlineNotice tone={notice.tone}>{notice.text}</InlineNotice>}

          <div className="border-t border-line/60 pt-4">
            <button type="button" onClick={() => void save()} disabled={saving} className={btnPrimary}>
              {saving ? "保存中…" : "保存摘要配置"}
            </button>
          </div>
        </div>
      )}
    </SectionCard>
  );
}

function clampHour(v: string): number {
  const n = Number(v);
  if (!Number.isInteger(n) || n < 0 || n > 23) {
    throw new Error("发送时刻需为 0-23 的整数（小时）");
  }
  return n;
}
