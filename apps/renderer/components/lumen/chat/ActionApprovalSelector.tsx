"use client";

import { useCallback, useRef, useState } from "react";
import { Check, ChevronDown, ShieldCheck, Zap } from "lucide-react";
import { useApprovalMode, type ApprovalMode } from "@/lib/hooks/useApprovalMode";
import { useDismiss } from "@/lib/hooks/useDismiss";
import { cn } from "@/lib/utils";

export const APPROVAL_OPTIONS: Array<{
  mode: ApprovalMode;
  title: string;
  description: string;
  icon: typeof ShieldCheck;
}> = [
  {
    mode: "approval",
    title: "Safe approvals",
    description: "Works on its own, then waits for you before consequential final actions.",
    icon: ShieldCheck
  },
  {
    mode: "autonomous",
    title: "Full autonomy",
    description: "Completes requested final actions without asking, including submits and sends. Payments still wait for you.",
    icon: Zap
  }
];

export function ActionApprovalSelector({ disabled }: { disabled: boolean }) {
  const [open, setOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const { mode, loading, saving, error, update } = useApprovalMode(true);
  const selected = APPROVAL_OPTIONS.find((option) => option.mode === mode) ?? APPROVAL_OPTIONS[0];
  const SelectedIcon = selected.icon;
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, close, [menuRef, triggerRef], triggerRef);
  const autonomous = mode === "autonomous";

  return (
    <div className="relative">
      <button
        ref={triggerRef}
        type="button"
        disabled={disabled || loading || saving}
        onClick={() => setOpen((value) => !value)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Approval mode: ${selected.title}`}
        className={cn(
          "inline-flex h-8 items-center gap-1.5 rounded-full border px-2.5 text-xs font-medium transition focus-ring",
          autonomous
            ? "border-attention/35 bg-attention/10 text-attention-ink hover:bg-attention/15"
            : "border-transparent text-fg-muted hover:bg-fg/[0.06] hover:text-fg",
          "disabled:cursor-not-allowed disabled:opacity-50"
        )}
      >
        <SelectedIcon className="size-3.5" />
        {loading ? "Loading…" : saving ? "Saving…" : selected.title}
        <ChevronDown className={cn("size-3 transition-transform duration-200", open && "rotate-180")} />
      </button>

      {open ? (
        <div
          ref={menuRef}
          role="menu"
          aria-label="How Lumen handles final actions"
          className="absolute bottom-full left-0 z-50 mb-2 w-[23rem] origin-bottom-left animate-enter-scale rounded-2xl border border-line-strong bg-popover p-1.5 shadow-lg"
        >
          <p className="px-3 pb-1.5 pt-2 text-xs font-medium text-fg-muted">When a task reaches its final step, Lumen…</p>
          {APPROVAL_OPTIONS.map((option) => {
            const Icon = option.icon;
            const active = option.mode === mode;
            const risky = option.mode === "autonomous";
            return (
              <button
                key={option.mode}
                type="button"
                role="menuitemradio"
                aria-checked={active}
                disabled={saving}
                onClick={async () => {
                  const saved = await update(option.mode);
                  if (saved) setOpen(false);
                }}
                className={cn(
                  "flex w-full items-start gap-3 rounded-xl px-3 py-2.5 text-left transition focus-ring-inset disabled:opacity-60",
                  active ? "bg-fg/[0.06]" : "hover:bg-fg/[0.04]"
                )}
              >
                <span
                  className={cn(
                    "mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-lg border",
                    risky ? "border-attention/30 bg-attention/10 text-attention-ink" : "border-brand/30 bg-brand/10 text-brand-ink"
                  )}
                >
                  <Icon className="size-3.5" />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-medium text-fg">{option.title}</span>
                  <span className="mt-0.5 block text-xs leading-[1.45] text-fg-muted">{option.description}</span>
                </span>
                <Check className={cn("mt-1 size-4 shrink-0 text-brand-ink transition-opacity", active ? "opacity-100" : "opacity-0")} />
              </button>
            );
          })}
          <p className="mx-3 mb-1.5 mt-1 border-t border-line pt-2.5 text-2xs leading-4 text-fg-subtle">
            Both modes still require grounded facts, an exactly bound target, and a verified outcome.
          </p>
          {error ? <p className="px-3 pb-2 text-xs text-danger-ink">{error}</p> : null}
        </div>
      ) : null}
    </div>
  );
}
