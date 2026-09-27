"use client";

/**
 * Provider 导入对话框（配置后台化）。
 *
 * 两种入口：
 * - mode="create"：「新增数据源」——说明界面仅支持配置已实现的数据源，
 *   新 Provider 需要对应实现类部署后端；可从 providers.yaml 导入已有配置。
 * - mode="import"：「从 YAML 导入」——确认框，含 overwrite 选项
 *   （覆盖 DB 中已保存的修改；凭据与后台修改的 overrides 始终保留）。
 *
 * 提交 → POST /providers/sync-from-yaml {overwrite}。
 * 关闭即卸载（open=false 时不渲染），状态随每次打开重置。
 */

import { useState } from "react";

import { apiClient } from "@/lib/api";
import { Modal } from "@/components/portfolio/Modal";

import { InlineNotice, btnGhost, btnPrimary } from "./ui";

interface ProviderImportDialogProps {
  open: boolean;
  mode: "create" | "import";
  onClose: () => void;
  /** 导入成功回调（父组件刷新列表并 toast） */
  onImported?: (imported: number, overwrite: boolean) => void;
}

export function ProviderImportDialog({ open, mode, onClose, onImported }: ProviderImportDialogProps) {
  // 关闭时不挂载：状态随打开重置；key 保证 create/import 切换时重新初始化
  if (!open) return null;
  return <ImportDialogInner key={mode} mode={mode} onClose={onClose} onImported={onImported} />;
}

function ImportDialogInner({
  mode,
  onClose,
  onImported,
}: {
  mode: "create" | "import";
  onClose: () => void;
  onImported?: (imported: number, overwrite: boolean) => void;
}) {
  const [overwrite, setOverwrite] = useState(false);
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function runImport() {
    setImporting(true);
    setError(null);
    try {
      const res = await apiClient.providers.syncFromYaml(overwrite);
      if (res.success === false) throw new Error(res.error?.message ?? "导入失败");
      const imported = res.data?.imported ?? 0;
      onImported?.(imported, overwrite);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "导入失败");
    } finally {
      setImporting(false);
    }
  }

  const isCreate = mode === "create";

  return (
    <Modal
      open
      onClose={onClose}
      title={isCreate ? "新增数据源" : "从 YAML 导入 Provider 配置"}
      description={
        isCreate ? "数据源的接入方式与限制说明" : "将 providers.yaml 中的配置同步到数据库并热生效"
      }
      footer={
        <>
          <button type="button" onClick={onClose} disabled={importing} className={btnGhost}>
            取消
          </button>
          <button
            type="button"
            onClick={() => void runImport()}
            disabled={importing}
            className={btnPrimary}
          >
            {importing ? "导入中…" : overwrite ? "覆盖导入" : "开始导入"}
          </button>
        </>
      }
    >
      <div className="space-y-3">
        {isCreate && (
          <div className="space-y-2 rounded-lg border border-accent/30 bg-accent/[0.06] px-3 py-2.5 text-xs leading-relaxed text-[#c9d1d9]">
            <p>
              界面<strong className="text-[#e6edf3]">不支持凭空创建数据源</strong>
              ：每个 Provider 需要后端有对应的实现类（抓取逻辑/鉴权方式），仅支持
              <strong className="text-[#e6edf3]">配置已实现的数据源</strong>。
            </p>
            <p className="text-muted">
              接入新数据源的步骤：在{" "}
              <code className="rounded bg-bg px-1 font-mono text-[10px]">
                backend/config/providers.yaml
              </code>{" "}
              中按现有格式添加配置并部署后端 → 回到本页点击「从 YAML 导入」→
              在列表中对新数据源点「配置」补全凭据与代理。
            </p>
          </div>
        )}

        <label className="flex cursor-pointer items-start gap-2.5 rounded-lg border border-line bg-bg px-3 py-2.5">
          <input
            type="checkbox"
            className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-[#58a6ff]"
            checked={overwrite}
            onChange={(e) => setOverwrite(e.target.checked)}
          />
          <span className="min-w-0 text-xs leading-relaxed">
            <span className="font-medium text-[#e6edf3]">覆盖数据库中的已有修改</span>
            <span className="mt-0.5 block text-muted">
              勾选后 YAML 中的 base_url/优先级/限流等将覆盖 DB
              中已保存的界面修改（已存储的加密凭据与 overrides 始终保留）；不勾选则仅导入数据库中尚不存在的数据源。
            </span>
          </span>
        </label>

        <p className="text-[11px] leading-relaxed text-muted">
          导入完成后立即生效：新数据源自动注册运行实例并加入主备优先级队列，无需重启。
        </p>

        {error && <InlineNotice tone="error">{error}</InlineNotice>}
      </div>
    </Modal>
  );
}
