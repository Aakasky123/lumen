"use client";

import { useEffect, useState } from "react";
import { Globe, Monitor, Pause, Play, Square } from "lucide-react";
import type { ActiveRun } from "@/lib/hooks/useActiveRun";
import { formatElapsed, formatTokens, runIsLive, runStatus } from "@/lib/format";
import { PHASE_META, runContextUsage, runPhase, runTargetLabel, type ContextUsage } from "@/lib/runPhase";
import { LumenMark } from "@/components/lumen/brand/LumenMark";
import { Button } from "@/components/ui/Button";
import { StatusDot } from "@/components/ui/badge";
import { Tooltip } from "@/components/ui/tooltip";
import { cn, parseBackendDate } from "@/lib/utils";

function ContextRing({ usage }: { usage: ContextUsage }) {
  const pct = usage.limitTokens ? Math.min(100, Math.round((usage.usedTokens / usage.limitTokens) * 100)) : null;
  const r = 7;
  const circumference = 2 * Math.PI * r;
  const color = pct == null ? "hsl(var(--fg-subtle))" : pct >= 90 ? "hsl(var(--danger))" : pct >= 75 ? "hsl(var(--attention))" : "hsl(var(--brand))";
  const label = usage.limitTokens
    ? `${usage.usedTokens.toLocaleString()} of ${usage.limitTokens.toLocaleString()} context tokens used${usage.model ? ` · ${usage.model}` : ""}`
    : `${usage.usedTokens.toLocaleString()} prompt tokens used${usage.model ? ` · ${usage.model}` : ""}; context window unknown`;
  return (
    <Tooltip label={label}>
      <span className="flex h-7 items-center gap-1.5 rounded-md px-1.5 text-2xs font-medium text-fg-subtle tabular" aria-label={label} role="img">
        <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
          <circle cx="9" cy="9" r={r} fill="none" stroke="hsl(var(--line-strong))" strokeWidth="2.25" />
          {pct != null ? (
            <circle
              cx="9"
              cy="9"
              r={r}
              fill="none"
              stroke={color}
              strokeWidth="2.25"
              strokeLinecap="round"
              strokeDasharray={`${(pct / 100) * circumference} ${circumference}`}
              transform="rotate(-90 9 9)"
              className="transition-[stroke-dasharray] duration-700"
            />
          ) : null}
        </svg>
        <span className="hidden lg:inline">{pct != null ? `${pct}%` : formatTokens(usage.usedTokens)}</span>
      </span>
    </Tooltip>
  );
}

function useElapsed(since: string | undefined, ticking: boolean): number | null {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!ticking) return;
    const interval = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(interval);
  }, [ticking]);
  if (!since) return null;
  const start = parseBackendDate(since).getTime();
  return Number.isFinite(start) ? Math.max(0, (now - start) / 1000) : null;
}

const DEFAULT_LINE: Record<string, string> = {
  working: "Working on it",
  paused: "Paused. Resume when you're ready.",
  needs_you: "Waiting for you"
};

/**
 * The live run, docked on top of the composer: what Lumen is doing right now (the run's own status
 * line), where, for how long, and the controls. Stop stays visible for as long as the run is live,
 * whatever you are typing, except while chat is locked (a secure entry, or the lock not yet known):
 * then the secure-entry card's own Cancel is the way out, and it also refreshes the lock.
 */
export function RunDock({ active, questionPending, allowStop }: { active: ActiveRun; questionPending: boolean; allowStop: boolean }) {
  const run = active.run;
  const phase = runPhase(run);
  const working = phase === "working";
  const elapsed = useElapsed(run?.created_at, working);
  if (!run || !phase || !runIsLive(run)) return null;

  const status = runStatus(run);
  const meta = PHASE_META[phase];
  const target = runTargetLabel(run.target_url);
  const desktop = run.target_url?.startsWith("desktop://");
  const usage = runContextUsage(run);
  const line =
    phase === "needs_you" && questionPending
      ? "Answer below to keep going"
      : run.status_message?.trim() || DEFAULT_LINE[phase] || "";

  return (
    <div className="animate-dock-in">
      <div className="flex min-h-12 items-center gap-3 px-3.5 py-2">
        <span className="flex size-7 shrink-0 items-center justify-center rounded-lg border border-line bg-surface-2">
          {working ? (
            <LumenMark className="size-4" working />
          ) : phase === "paused" ? (
            <Pause className="size-3.5 text-fg-muted" />
          ) : (
            <StatusDot tone="attention" pulse />
          )}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span
              role="status"
              aria-live="polite"
              className={cn("text-13 font-semibold", meta.tone === "attention" ? "text-attention-ink" : meta.tone === "brand" ? "text-brand-ink" : "text-fg")}
            >
              {meta.label}
            </span>
            {target ? (
              <span className="flex min-w-0 items-center gap-1 text-xs text-fg-subtle">
                {desktop ? <Monitor className="size-3 shrink-0" /> : <Globe className="size-3 shrink-0" />}
                <span className="truncate">{target}</span>
              </span>
            ) : null}
            {working && elapsed != null ? <span className="text-xs text-fg-subtle tabular">· {formatElapsed(elapsed)}</span> : null}
          </div>
          <p key={line} className={cn("truncate text-13 text-fg-muted animate-enter-fade", working && "text-fg/80")} title={line}>
            {line}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {usage ? <ContextRing usage={usage} /> : null}
          {status.canResume ? (
            <Button variant="primary" size="sm" onClick={active.resume} loading={active.busy} className="ml-1">
              {!active.busy ? <Play className="fill-current" /> : null}
              Resume
            </Button>
          ) : null}
          {status.canPause ? (
            <Tooltip label="Pause">
              <Button variant="ghost" size="icon-sm" aria-label="Pause the task" onClick={active.pause} disabled={active.busy}>
                <Pause />
              </Button>
            </Tooltip>
          ) : null}
          {status.canEnd && allowStop ? (
            <Tooltip label="Stop the task. Lumen leaves the page as it is.">
              <Button variant="danger-ghost" size="sm" aria-label="Stop the task" onClick={active.end} disabled={active.busy} className="gap-1.5 px-2.5">
                <Square className="size-3 fill-current" />
                Stop
              </Button>
            </Tooltip>
          ) : null}
        </div>
      </div>
    </div>
  );
}
