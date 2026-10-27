import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva("inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-full font-medium leading-none [&_svg]:size-3", {
  variants: {
    tone: {
      neutral: "bg-fg/[0.07] text-fg-muted",
      outline: "border border-line-strong text-fg-muted",
      brand: "bg-brand/12 text-brand-ink",
      attention: "bg-attention/14 text-attention-ink",
      danger: "bg-danger/12 text-danger-ink"
    },
    size: {
      sm: "h-5 px-2 text-2xs",
      md: "h-6 px-2.5 text-xs"
    }
  },
  defaultVariants: { tone: "neutral", size: "sm" }
});

export function Badge({ className, tone, size, ...props }: React.HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badgeVariants>) {
  return <span className={cn(badgeVariants({ tone, size }), className)} {...props} />;
}

export function Kbd({ className, children }: { className?: string; children: React.ReactNode }) {
  return (
    <kbd
      className={cn(
        "inline-flex h-5 min-w-5 items-center justify-center rounded-[5px] border border-line-strong bg-surface-2 px-1 text-[10.5px] font-medium leading-none text-fg-muted shadow-xs",
        className
      )}
    >
      {children}
    </kbd>
  );
}

/** A tiny state dot. `pulse` adds a soft expanding ring for states that want your attention. */
export function StatusDot({ tone = "neutral", pulse = false, className }: { tone?: "neutral" | "brand" | "attention" | "danger"; pulse?: boolean; className?: string }) {
  const color = {
    neutral: "bg-fg-subtle",
    brand: "bg-brand",
    attention: "bg-attention",
    danger: "bg-danger"
  }[tone];
  return (
    <span className={cn("relative inline-flex size-2 shrink-0", className)} aria-hidden="true">
      {pulse ? <span className={cn("absolute inset-0 rounded-full animate-ping-soft", color)} /> : null}
      <span className={cn("relative size-2 rounded-full", color)} />
    </span>
  );
}
