import * as React from "react";
import { cn } from "@/lib/utils";

export function Switch({
  checked,
  onCheckedChange,
  disabled,
  tone = "brand",
  className,
  ...props
}: Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "onChange"> & {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  /** attention marks a switch that widens what Lumen may do on its own. */
  tone?: "brand" | "attention";
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onCheckedChange(!checked)}
      className={cn(
        "relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors duration-200 focus-ring disabled:opacity-45",
        checked
          ? tone === "attention"
            ? "border-attention/60 bg-attention"
            : "border-brand/60 bg-brand"
          : "border-line-strong bg-surface-3",
        className
      )}
      {...props}
    >
      <span
        aria-hidden="true"
        className={cn(
          "absolute left-0.5 size-3.5 rounded-full shadow-sm transition-transform duration-200 ease-out",
          checked ? "translate-x-4 bg-white" : "translate-x-0 bg-fg-muted"
        )}
      />
    </button>
  );
}
