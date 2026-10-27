"use client";

import { useEffect, useSyncExternalStore } from "react";
import { AlertCircle, CheckCircle2, X } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Minimal toasts: a module-level store any component can push to, one viewport mounted at the
 * root. Confirmation of quiet saves ("Profile saved") and of failures that have no inline home.
 */

type Toast = { id: number; tone: "success" | "error" | "neutral"; title: string; detail?: string };

let toasts: Toast[] = [];
let nextId = 1;
const listeners = new Set<() => void>();

function emit() {
  for (const listener of listeners) listener();
}

function dismiss(id: number) {
  toasts = toasts.filter((item) => item.id !== id);
  emit();
}

export function toast(title: string, options: { tone?: Toast["tone"]; detail?: string } = {}) {
  const item: Toast = { id: nextId++, tone: options.tone ?? "success", title, detail: options.detail };
  toasts = [...toasts.slice(-2), item];
  emit();
  return item.id;
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

const getSnapshot = () => toasts;
const EMPTY: Toast[] = [];

function ToastItem({ item }: { item: Toast }) {
  useEffect(() => {
    const timeout = window.setTimeout(() => dismiss(item.id), item.tone === "error" ? 7000 : 3600);
    return () => window.clearTimeout(timeout);
  }, [item.id, item.tone]);

  const Icon = item.tone === "error" ? AlertCircle : CheckCircle2;
  return (
    <div
      role={item.tone === "error" ? "alert" : "status"}
      className="pointer-events-auto flex w-[22rem] max-w-[calc(100vw-2rem)] animate-enter-up items-start gap-3 rounded-xl border border-line-strong bg-popover p-3 pr-2 shadow-lg"
    >
      <Icon className={cn("mt-0.5 size-4 shrink-0", item.tone === "error" ? "text-danger-ink" : item.tone === "success" ? "text-brand-ink" : "text-fg-muted")} />
      <div className="min-w-0 flex-1">
        <p className="text-13 font-medium text-fg">{item.title}</p>
        {item.detail ? <p className="mt-0.5 text-xs text-fg-muted">{item.detail}</p> : null}
      </div>
      <button
        type="button"
        onClick={() => dismiss(item.id)}
        aria-label="Dismiss"
        className="flex size-6 shrink-0 items-center justify-center rounded-sm text-fg-subtle transition hover:bg-fg/[0.06] hover:text-fg focus-ring"
      >
        <X className="size-3.5" />
      </button>
    </div>
  );
}

export function Toaster() {
  const items = useSyncExternalStore(subscribe, getSnapshot, () => EMPTY);
  return (
    <div aria-live="polite" className="pointer-events-none fixed bottom-5 right-5 z-[80] flex flex-col items-end gap-2">
      {items.map((item) => (
        <ToastItem key={item.id} item={item} />
      ))}
    </div>
  );
}
