"use client";

/**
 * 轻量页面级 toast：useToast() 产出状态，<ToastHost /> 固定右下角渲染。
 * 成功 / 失败内联提示的替代实现（无全局 Provider，页面内自包含）。
 */

import { useCallback, useRef, useState } from "react";

export interface ToastMessage {
  kind: "success" | "error";
  text: string;
}

export function useToast() {
  const [toast, setToast] = useState<ToastMessage | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const show = useCallback((kind: ToastMessage["kind"], text: string) => {
    if (timerRef.current) clearTimeout(timerRef.current);
    setToast({ kind, text });
    timerRef.current = setTimeout(() => setToast(null), 3400);
  }, []);

  return { toast, show };
}

export function ToastHost({ toast }: { toast: ToastMessage | null }) {
  if (!toast) return null;
  const ok = toast.kind === "success";
  return (
    <div
      className={`animate-fade-up fixed bottom-5 right-5 z-[60] max-w-xs rounded-lg border px-4 py-3 text-xs leading-relaxed shadow-[0_12px_36px_-8px_rgba(0,0,0,0.6)] backdrop-blur ${
        ok
          ? "border-up/50 bg-[#12261a]/95 text-up"
          : "border-down/50 bg-[#2a1512]/95 text-down"
      }`}
      role="status"
      aria-live="polite"
    >
      <span aria-hidden className="mr-1.5">
        {ok ? "✓" : "✕"}
      </span>
      {toast.text}
    </div>
  );
}
