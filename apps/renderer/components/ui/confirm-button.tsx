"use client";

import { useEffect, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/Button";

/**
 * A destructive action that takes two clicks: the first arms it (and says what will happen), the
 * second does it. Disarms itself after a few seconds or when focus leaves.
 */
export function ConfirmButton({
  onConfirm,
  children,
  confirmLabel = "Delete?",
  size = "xs",
  icon,
  ariaLabel,
  disabled
}: {
  onConfirm: () => void | Promise<void>;
  children?: ReactNode;
  confirmLabel?: string;
  size?: "xs" | "sm" | "icon-xs" | "icon-sm";
  icon?: ReactNode;
  ariaLabel?: string;
  disabled?: boolean;
}) {
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!armed) return;
    const timeout = window.setTimeout(() => setArmed(false), 4000);
    return () => window.clearTimeout(timeout);
  }, [armed]);

  if (armed) {
    return (
      <Button
        variant="danger"
        size={size === "icon-xs" ? "xs" : size === "icon-sm" ? "sm" : size}
        loading={busy}
        autoFocus
        onBlur={() => !busy && setArmed(false)}
        onClick={async () => {
          setBusy(true);
          try {
            await onConfirm();
          } finally {
            setBusy(false);
            setArmed(false);
          }
        }}
      >
        {confirmLabel}
      </Button>
    );
  }
  return (
    <Button variant="danger-ghost" size={size} aria-label={ariaLabel} disabled={disabled} onClick={() => setArmed(true)}>
      {icon}
      {children}
    </Button>
  );
}
