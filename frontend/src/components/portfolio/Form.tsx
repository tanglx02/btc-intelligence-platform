"use client";

/**
 * 表单基础件：Field / TextInput / NumberInput / SelectInput / DateInput。
 * 统一暗色终端风格；错误信息内联展示。
 */

import type {
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
} from "react";

import { cn } from "./utils";

const CONTROL_CLASS =
  "w-full rounded-lg border border-line bg-bg px-3 py-2 text-sm text-[#e6edf3] placeholder:text-muted/50 outline-none transition-colors focus:border-accent/60 disabled:opacity-50";

export function Field({
  label,
  hint,
  error,
  required,
  children,
  className = "",
}: {
  label: string;
  hint?: string;
  error?: string | null;
  required?: boolean;
  children: ReactNode;
  className?: string;
}) {
  return (
    <label className={cn("block", className)}>
      <span className="mb-1.5 flex items-baseline gap-1.5 text-[11px] font-medium text-muted">
        {label}
        {required && <span aria-hidden className="text-accent">*</span>}
        {hint && <span className="font-normal text-muted/60">{hint}</span>}
      </span>
      {children}
      {error && <span className="mt-1 block text-[11px] text-down">{error}</span>}
    </label>
  );
}

export function TextInput(props: InputHTMLAttributes<HTMLInputElement>) {
  const { className = "", ...rest } = props;
  return <input type="text" {...rest} className={cn(CONTROL_CLASS, className)} />;
}

export function NumberInput(props: InputHTMLAttributes<HTMLInputElement>) {
  const { className = "", ...rest } = props;
  return (
    <input
      type="number"
      inputMode="decimal"
      min={0}
      step="any"
      {...rest}
      className={cn(CONTROL_CLASS, "font-mono tabular-nums", className)}
    />
  );
}

export function DateInput(props: InputHTMLAttributes<HTMLInputElement>) {
  const { className = "", ...rest } = props;
  return <input type="date" {...rest} className={cn(CONTROL_CLASS, "font-mono", className)} />;
}

export function SelectInput({
  options,
  className = "",
  ...rest
}: SelectHTMLAttributes<HTMLSelectElement> & {
  options: { value: string; label: string }[];
}) {
  return (
    <select {...rest} className={cn(CONTROL_CLASS, "appearance-none pr-8", className)}>
      {options.map((o) => (
        <option key={o.value} value={o.value} className="bg-bg-raised">
          {o.label}
        </option>
      ))}
    </select>
  );
}

/** 表单级错误横幅（提交失败内联提示） */
export function FormError({ message }: { message?: string | null }) {
  if (!message) return null;
  return (
    <div
      className="animate-fade-up rounded-lg border border-down/40 bg-down/10 px-3 py-2 text-xs text-down"
      role="alert"
    >
      {message}
    </div>
  );
}
