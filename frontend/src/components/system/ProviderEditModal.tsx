"use client";

/**
 * Provider 配置编辑模态框（配置后台化）。
 *
 * - 编辑已实现数据源的连接 / 凭据 / 限流 / 优先级 / 锁定 / 启用配置
 * - 保存 → PUT /providers/{name}（落库 + 热重载生效，响应掩码）
 * - 凭据（API Key/Secret/Passphrase）一律 password 输入框：已配置显示掩码占位，
 *   留空 = 保留旧值（后端约定），非空 = 加密存储
 * - 「重载」→ POST /providers/{name}/reload（重建实例，用于保存后手动刷新）
 * - 每个字段带 ℹ️ 说明（Field hint）；表单校验：URL 格式、代理协议、数字范围
 *
 * 关闭即卸载（open=false 时不渲染），表单状态随每次打开重置；无需 effect 水合。
 */

import { useState } from "react";

import { apiClient } from "@/lib/api";
import { Modal } from "@/components/portfolio/Modal";

import {
  Field,
  InlineNotice,
  PROVIDER_CATEGORY_OPTIONS,
  ToggleSwitch,
  btnGhost,
  btnPrimary,
  categoryLabel,
  inputClass,
} from "./ui";
import type { ProviderRow } from "./types";

import type { ProviderConfigUpdateBody } from "@/types/api";

/* -------------------------------------------------------------------------- */
/* 表单状态                                                                    */
/* -------------------------------------------------------------------------- */

interface FormState {
  baseUrl: string;
  apiKey: string;
  apiSecret: string;
  apiPassphrase: string;
  priority: string;
  proxy: string;
  timeoutSeconds: string;
  retryCount: string;
  ratePerMinute: string;
  enabled: boolean;
  locked: boolean;
}

function str(v: unknown): string {
  return v === null || v === undefined ? "" : String(v);
}

/** 从 ProviderRow（含掩码 config）初始化表单；凭据永远置空（掩码占位） */
function formFromProvider(row: ProviderRow): FormState {
  const cfg = row.config ?? {};
  const connect = cfg.timeout_config?.connect;
  const retryCount = cfg.retry_config?.count;
  const requests = cfg.rate_limit?.requests;
  return {
    baseUrl: str(cfg.base_url ?? row.base_url),
    apiKey: "",
    apiSecret: "",
    apiPassphrase: "",
    priority: str(row.priority ?? cfg.priority ?? ""),
    proxy: str(cfg.proxy),
    timeoutSeconds: connect === null || connect === undefined ? "" : String(connect),
    retryCount: retryCount === null || retryCount === undefined ? "" : String(retryCount),
    ratePerMinute: requests === null || requests === undefined ? "" : String(requests),
    enabled: row.is_enabled ?? cfg.is_enabled ?? true,
    locked: cfg.is_locked === true,
  };
}

/* -------------------------------------------------------------------------- */
/* 校验                                                                       */
/* -------------------------------------------------------------------------- */

function validate(form: FormState): string | null {
  const baseUrl = form.baseUrl.trim();
  if (baseUrl) {
    try {
      const u = new URL(baseUrl);
      if (u.protocol !== "http:" && u.protocol !== "https:") {
        return "base_url 需为 http(s):// 开头的完整 URL";
      }
    } catch {
      return "base_url 格式不正确（示例：https://api.example.com）";
    }
  }
  const proxy = form.proxy.trim();
  if (proxy && !/^(https?|socks5):\/\//i.test(proxy)) {
    return "代理地址需以 http://、https:// 或 socks5:// 开头";
  }
  const priority = form.priority.trim();
  if (!priority) return "优先级为必填项（数字越小越优先使用）";
  const p = Number(priority);
  if (!Number.isInteger(p) || p < 0) return "优先级需为 ≥ 0 的整数";

  if (form.timeoutSeconds.trim()) {
    const t = Number(form.timeoutSeconds);
    if (!Number.isFinite(t) || t <= 0) return "连接超时需为大于 0 的数字（秒）";
  }
  if (form.retryCount.trim()) {
    const r = Number(form.retryCount);
    if (!Number.isInteger(r) || r < 0) return "重试次数需为 ≥ 0 的整数";
  }
  if (form.ratePerMinute.trim()) {
    const r = Number(form.ratePerMinute);
    if (!Number.isInteger(r) || r <= 0) return "限流需为大于 0 的整数（每分钟请求数）";
  }
  return null;
}

/* -------------------------------------------------------------------------- */
/* 请求体构造（仅提交有变化的字段；凭据非空才提交，空 = 保留旧值）              */
/* -------------------------------------------------------------------------- */

function buildBody(form: FormState, row: ProviderRow): ProviderConfigUpdateBody {
  const cfg = row.config ?? {};
  const body: ProviderConfigUpdateBody = {
    priority: Number(form.priority.trim()),
    is_enabled: form.enabled,
    is_locked: form.locked,
  };

  const baseUrl = form.baseUrl.trim();
  if (baseUrl !== str(cfg.base_url ?? row.base_url)) body.base_url = baseUrl;

  const proxy = form.proxy.trim();
  if (proxy !== str(cfg.proxy)) body.proxy = proxy; // 空串 = 清除代理（后端转 direct）

  const timeout = form.timeoutSeconds.trim();
  if (timeout && Number(timeout) !== cfg.timeout_config?.connect) {
    // 标量提交：后端归一化为 connect/read/write/pool 四段同值
    body.timeout_config = Number(timeout);
  }

  const retry = form.retryCount.trim();
  if (retry && Number(retry) !== cfg.retry_config?.count) {
    body.retry_config = { ...(cfg.retry_config ?? {}), count: Number(retry) };
  }

  const rate = form.ratePerMinute.trim();
  if (rate && Number(rate) !== cfg.rate_limit?.requests) {
    body.rate_limit = { requests: Number(rate), period: 60 };
  }

  if (form.apiKey.trim()) body.api_key = form.apiKey.trim();
  if (form.apiSecret.trim()) body.api_secret = form.apiSecret.trim();
  if (form.apiPassphrase.trim()) body.api_passphrase = form.apiPassphrase.trim();

  return body;
}

/* -------------------------------------------------------------------------- */
/* 组件                                                                       */
/* -------------------------------------------------------------------------- */

interface ProviderEditModalProps {
  open: boolean;
  /** 目标 Provider 行（null 时不渲染内容） */
  provider: ProviderRow | null;
  onClose: () => void;
  /** 保存成功后回调（父组件刷新列表并 toast） */
  onSaved?: (message: string) => void;
}

export function ProviderEditModal({ open, provider, onClose, onSaved }: ProviderEditModalProps) {
  // 关闭时不挂载：状态随打开重置；key 保证切换 Provider 时重新初始化
  if (!open || !provider) return null;
  return (
    <ProviderEditModalInner
      key={provider.name}
      provider={provider}
      onClose={onClose}
      onSaved={onSaved}
    />
  );
}

function ProviderEditModalInner({
  provider,
  onClose,
  onSaved,
}: {
  provider: ProviderRow;
  onClose: () => void;
  onSaved?: (message: string) => void;
}) {
  const [form, setForm] = useState<FormState>(() => formFromProvider(provider));
  const [saving, setSaving] = useState(false);
  const [reloading, setReloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadNote, setReloadNote] = useState<string | null>(null);

  function patch<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  async function save() {
    const invalid = validate(form);
    if (invalid) {
      setError(invalid);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const res = await apiClient.providers.updateProvider(provider.name, buildBody(form, provider));
      if (res.success === false) throw new Error(res.error?.message ?? "保存失败");
      onSaved?.("配置已保存并生效");
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  async function reload() {
    setReloading(true);
    setError(null);
    setReloadNote(null);
    try {
      const res = await apiClient.providers.reloadProvider(provider.name);
      if (res.success === false) throw new Error(res.error?.message ?? "重载失败");
      setReloadNote(
        res.data?.reloaded
          ? "已重建 Provider 实例并重载生效"
          : (res.data?.note ?? "运行时状态已同步"),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "重载失败");
    } finally {
      setReloading(false);
    }
  }

  const cfg = provider.config ?? {};
  const keySet = typeof cfg.api_key === "string" && cfg.api_key.length > 0;
  const secretSet = typeof cfg.api_secret === "string" && cfg.api_secret.length > 0;
  const passphraseSet = typeof cfg.api_passphrase === "string" && cfg.api_passphrase.length > 0;

  return (
    <Modal
      open
      onClose={onClose}
      wide
      title={`配置数据源 · ${provider.name}`}
      description="修改保存后立即落库并热重载生效，无需重启服务；凭据留空表示保留已存储的旧值。"
      footer={
        <>
          <button
            type="button"
            onClick={() => void reload()}
            disabled={reloading || saving}
            className={btnGhost}
            title="重建 Provider 实例（用于保存后手动刷新运行状态）"
          >
            {reloading ? "重载中…" : "重载"}
          </button>
          <button type="button" onClick={onClose} disabled={saving} className={btnGhost}>
            取消
          </button>
          <button type="button" onClick={() => void save()} disabled={saving} className={btnPrimary}>
            {saving ? "保存中…" : "保存并生效"}
          </button>
        </>
      }
    >
      <div className="space-y-4">
        {/* 元信息：配置来源 / 锁定状态 */}
        <div className="flex flex-wrap items-center gap-2 text-[11px]">
          <span className="rounded-lg border border-line bg-bg px-2.5 py-1 text-muted">
            类别{" "}
            <span className="font-mono font-semibold text-[#e6edf3]">
              {categoryLabel(provider.category)}
            </span>
          </span>
          {cfg.config_source && (
            <span className="rounded-lg border border-line bg-bg px-2.5 py-1 text-muted">
              配置来源{" "}
              <span className="font-mono font-semibold text-[#e6edf3]">{cfg.config_source}</span>
            </span>
          )}
          {cfg.is_locked === true && (
            <span className="rounded-lg border border-accent/40 bg-accent/10 px-2.5 py-1 text-accent">
              优先级已锁定
            </span>
          )}
        </div>

        <div className="grid grid-cols-1 gap-x-5 gap-y-4 sm:grid-cols-2">
          <Field label="显示名称">
            <input
              className={inputClass}
              value={provider.name}
              disabled
              title="名称为 Provider 唯一标识（slug），不可修改"
            />
          </Field>
          <Field label="base_url" hint="数据源 API 根地址，如 https://api.example.com">
            <input
              className={inputClass}
              value={form.baseUrl}
              onChange={(e) => patch("baseUrl", e.target.value)}
              placeholder="https://api.example.com"
              autoComplete="off"
              spellCheck={false}
            />
          </Field>
          <Field
            label="API Key"
            hint={keySet ? "已配置（掩码），留空保留旧值" : "未配置；留空表示不设置"}
          >
            <input
              className={inputClass}
              type="password"
              value={form.apiKey}
              onChange={(e) => patch("apiKey", e.target.value)}
              placeholder={keySet ? "••••••••（留空保留）" : "输入 API Key"}
              autoComplete="new-password"
            />
          </Field>
          <Field
            label="API Secret"
            hint={secretSet ? "已配置（掩码），留空保留旧值" : "未配置；留空表示不设置"}
          >
            <input
              className={inputClass}
              type="password"
              value={form.apiSecret}
              onChange={(e) => patch("apiSecret", e.target.value)}
              placeholder={secretSet ? "••••••••（留空保留）" : "输入 API Secret"}
              autoComplete="new-password"
            />
          </Field>
          <Field label="Passphrase（可选）" hint="仅 OKX 等需要口令短语的数据源使用">
            <input
              className={inputClass}
              type="password"
              value={form.apiPassphrase}
              onChange={(e) => patch("apiPassphrase", e.target.value)}
              placeholder={passphraseSet ? "••••••••（留空保留）" : "输入 Passphrase"}
              autoComplete="new-password"
            />
          </Field>
          <Field label="优先级" hint="数字越小越优先使用；主数据源失败后按此顺序切换">
            <input
              className={inputClass}
              value={form.priority}
              onChange={(e) => patch("priority", e.target.value)}
              inputMode="numeric"
              placeholder="1"
            />
          </Field>
          <Field
            label="代理 URL（可选）"
            hint="如 http://127.0.0.1:7890 或 socks5://…，用于访问境外 API"
          >
            <input
              className={inputClass}
              value={form.proxy}
              onChange={(e) => patch("proxy", e.target.value)}
              placeholder="http://127.0.0.1:7890"
              autoComplete="off"
              spellCheck={false}
            />
          </Field>
          <Field label="连接超时（秒）" hint="连接/读取/写入统一应用该值；留空保持现状">
            <input
              className={inputClass}
              value={form.timeoutSeconds}
              onChange={(e) => patch("timeoutSeconds", e.target.value)}
              inputMode="decimal"
              placeholder="10"
            />
          </Field>
          <Field label="重试次数" hint="请求失败后的最大重试次数；留空保持现状">
            <input
              className={inputClass}
              value={form.retryCount}
              onChange={(e) => patch("retryCount", e.target.value)}
              inputMode="numeric"
              placeholder="3"
            />
          </Field>
          <Field
            label="限流（每分钟请求数）"
            hint="每分钟最大请求数，防止触发数据源的频率限制"
          >
            <input
              className={inputClass}
              value={form.ratePerMinute}
              onChange={(e) => patch("ratePerMinute", e.target.value)}
              inputMode="numeric"
              placeholder="60"
            />
          </Field>
          <Field label="启用" hint="禁用后不参与数据抓取与主备切换">
            <div className="flex h-[38px] items-center gap-2 rounded-lg border border-line bg-bg px-3">
              <ToggleSwitch
                checked={form.enabled}
                onChange={(next) => patch("enabled", next)}
                title="启用后参与数据抓取与主备切换"
              />
              <span className="text-xs text-[#e6edf3]">{form.enabled ? "已启用" : "已禁用"}</span>
            </div>
          </Field>
          <Field label="锁定优先级" hint="锁定后系统不再根据健康评分自动调整该数据源的优先级">
            <div className="flex h-[38px] items-center gap-2 rounded-lg border border-line bg-bg px-3">
              <ToggleSwitch
                checked={form.locked}
                onChange={(next) => patch("locked", next)}
                title="锁定 = 优先级固定，不参与自动调整"
              />
              <span className="text-xs text-[#e6edf3]">{form.locked ? "已锁定" : "未锁定"}</span>
            </div>
          </Field>
        </div>

        {/* 类别（不可修改，仅展示，保证 9 类下拉语义完整） */}
        <Field label="数据类别" hint="类别由后端实现类定义，不可在界面修改">
          <select className={inputClass} value={(provider.category ?? "").toUpperCase()} disabled>
            {PROVIDER_CATEGORY_OPTIONS.map((c) => (
              <option key={c} value={c} className="bg-bg-raised">
                {categoryLabel(c)}
              </option>
            ))}
          </select>
        </Field>

        {error && <InlineNotice tone="error">{error}</InlineNotice>}
        {reloadNote && <InlineNotice tone="info">{reloadNote}</InlineNotice>}
      </div>
    </Modal>
  );
}
