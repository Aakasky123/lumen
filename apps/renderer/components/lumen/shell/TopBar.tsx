"use client";

import type { ReactNode } from "react";
import { Globe, Monitor } from "lucide-react";
import { runTargetLabel } from "@/lib/runPhase";

export function TopBar({ title, targetUrl, cost, right }: { title: string; targetUrl?: string | null; cost?: string | null; right?: ReactNode }) {
  const target = runTargetLabel(targetUrl);
  const desktop = Boolean(targetUrl?.startsWith("desktop://"));
  return (
    <header className="flex h-14 shrink-0 items-center gap-3 px-5">
      <div className="flex min-w-0 flex-1 items-center gap-2.5">
        {title ? <h1 className="truncate text-sm font-medium text-fg">{title}</h1> : null}
        {target ? (
          <span className="flex shrink-0 items-center gap-1.5 rounded-full border border-line px-2 py-0.5 text-2xs font-medium text-fg-muted">
            {desktop ? <Monitor className="size-3" /> : <Globe className="size-3" />}
            {target}
          </span>
        ) : null}
        {cost ? (
          <span className="shrink-0 text-xs tabular-nums text-fg-subtle" title="Model spend for this task">
            {cost}
          </span>
        ) : null}
      </div>
      {right ? <div className="flex shrink-0 items-center gap-2">{right}</div> : null}
    </header>
  );
}
