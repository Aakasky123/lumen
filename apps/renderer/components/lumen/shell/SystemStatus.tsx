"use client";

import { useCallback, useRef, useState } from "react";
import { AlertTriangle, Chrome, Cpu, FolderOpen, MousePointer2, RotateCw, Server } from "lucide-react";
import type { DesktopStatusState } from "@/lib/hooks/useDesktopStatus";
import { useDismiss } from "@/lib/hooks/useDismiss";
import { Button } from "@/components/ui/Button";
import { StatusDot } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";
import { desktopName, useIsMac } from "@/lib/platform";

type Row = { icon: typeof Server; label: string; value: string; tone: "brand" | "attention" | "neutral" | "danger"; title?: string };

function rows(status: LumenDesktopStatus | null): Row[] {
  return [
    {
      icon: Server,
      label: "Runtime",
      value: status?.backendHealthy ? "Running" : "Offline",
      tone: status?.backendHealthy ? "brand" : "danger"
    },
    {
      icon: Cpu,
      label: "Model",
      value: status?.modelReady ? status.modelDisplayName || "Ready" : status?.modelConfigured ? "Not responding" : "Not connected",
      tone: status?.modelReady ? "brand" : "attention",
      title: status?.modelError || status?.modelDisplayName || ""
    },
    {
      icon: Chrome,
      label: "Chrome",
      value: status?.liveActionsReady ? "Connected" : status?.extensionConnected ? "Needs a reload" : "Opens when needed",
      tone: status?.liveActionsReady ? "brand" : status?.extensionConnected ? "attention" : "neutral"
    }
  ];
}

/**
 * Desktop host health as one quiet pill: a dot and "Ready" when everything is fine, the one problem
 * worth fixing (and its fix) when not. Detail and host actions open on click.
 */
export function SystemStatus({ desktop, onOpenModels, compact = false }: { desktop: DesktopStatusState; onOpenModels: () => void; compact?: boolean }) {
  const isMac = useIsMac();
  const [open, setOpen] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, close, [panelRef, triggerRef], triggerRef);
  const { status, problem, busy } = desktop;
  if (!desktop.available) return null;

  const loading = !status;
  const tone = loading ? "neutral" : problem ? (problem.kind === "runtime" ? "danger" : "attention") : "brand";

  return (
    <div className="relative">
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        aria-haspopup="dialog"
        className={cn(
          "flex h-8 max-w-[22rem] items-center gap-2 rounded-full border px-3 text-xs font-medium transition focus-ring",
          problem
            ? problem.kind === "runtime"
              ? "border-danger/35 bg-danger/10 text-danger-ink hover:bg-danger/15"
              : "border-attention/35 bg-attention/10 text-attention-ink hover:bg-attention/15"
            : "border-line bg-transparent text-fg-muted hover:bg-fg/[0.05] hover:text-fg"
        )}
      >
        {problem ? <AlertTriangle className="size-3.5 shrink-0" /> : <StatusDot tone={tone} />}
        <span className={cn("truncate", compact && !problem && "sr-only")}>{loading ? "Checking…" : problem ? problem.text : "Ready"}</span>
      </button>

      {open ? (
        <div
          ref={panelRef}
          role="dialog"
          aria-label="Lumen desktop status"
          className="absolute right-0 top-full z-50 mt-2 w-80 origin-top-right animate-enter-scale rounded-2xl border border-line-strong bg-popover p-1.5 shadow-lg"
        >
          {problem ? (
            <div className={cn("mb-1.5 rounded-xl px-3.5 py-3", problem.kind === "runtime" ? "bg-danger/[0.08]" : "bg-attention/[0.08]")}>
              <p className={cn("text-13 font-semibold", problem.kind === "runtime" ? "text-danger-ink" : "text-attention-ink")}>{problem.text}</p>
              <p className="mt-0.5 text-xs text-fg-muted">{problem.detail}</p>
              {problem.action || problem.kind.startsWith("model") ? (
                <Button
                  size="xs"
                  variant="secondary"
                  className="mt-2.5"
                  loading={busy && problem.kind === "runtime"}
                  onClick={() => {
                    if (problem.action) problem.action.run();
                    else {
                      setOpen(false);
                      onOpenModels();
                    }
                  }}
                >
                  {problem.action?.label ?? "Open model settings"}
                </Button>
              ) : null}
            </div>
          ) : null}
          <ul className="px-1.5 py-1">
            {rows(status).map((row) => (
              <li key={row.label} className="flex h-9 items-center gap-3 text-13" title={row.title}>
                <row.icon className="size-4 shrink-0 text-fg-subtle" />
                <span className="text-fg-muted">{row.label}</span>
                <span className="ml-auto flex min-w-0 items-center gap-2 text-fg">
                  <span className="truncate">{row.value}</span>
                  <StatusDot tone={row.tone} />
                </span>
              </li>
            ))}
            <li className="flex h-10 items-center gap-3 text-13">
              <MousePointer2 className="size-4 shrink-0 text-fg-subtle" />
              <span className="text-fg-muted">Computer use</span>
              <span className="ml-auto flex items-center gap-2.5">
                <span className="text-xs text-fg-subtle">{status?.computerUseAvailable ? (status.computerUseEnabled ? "On" : "Paused") : "Unavailable"}</span>
                <Switch
                  checked={Boolean(status?.computerUseEnabled)}
                  disabled={busy || !status?.computerUseAvailable}
                  onCheckedChange={(next) => void desktop.setComputerUse(next)}
                  aria-label={`Let Lumen use ${desktopName(isMac)} apps`}
                />
              </span>
            </li>
          </ul>
          <div className="mt-1 grid grid-cols-3 gap-1 border-t border-line p-1 pt-2">
            <Button variant="ghost" size="xs" className="h-12 flex-col gap-1 py-1.5 text-2xs" onClick={() => void desktop.runRuntime(status?.backendHealthy ? "restart" : "start")} disabled={busy}>
              <RotateCw className={cn(busy && "animate-spin")} />
              {status?.backendHealthy ? "Restart" : "Start"}
            </Button>
            <Button variant="ghost" size="xs" className="h-12 flex-col gap-1 py-1.5 text-2xs" onClick={desktop.openChromeExtensions}>
              <Chrome />
              Extensions
            </Button>
            <Button variant="ghost" size="xs" className="h-12 flex-col gap-1 py-1.5 text-2xs" onClick={desktop.openDataFolder}>
              <FolderOpen />
              Data folder
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
