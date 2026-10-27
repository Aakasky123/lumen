"use client";

import { useCallback, useEffect, useState } from "react";
import { desktopBridge } from "@/lib/desktop";

export type HostProblem = {
  kind: "runtime" | "model-missing" | "model-unhealthy" | "extension";
  text: string;
  detail: string;
  action?: { label: string; run: () => void };
};

/**
 * Desktop host health (runtime, model, Chrome body, computer use), polled every 5 s while the
 * cockpit runs inside the desktop app. `available` is false in a plain browser.
 */
export function useDesktopStatus() {
  const [available, setAvailable] = useState(false);
  const [status, setStatus] = useState<LumenDesktopStatus | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    const bridge = desktopBridge();
    if (!bridge) return;
    try {
      setStatus(await bridge.getStatus());
    } catch {
      setStatus((current) => (current ? { ...current, backendHealthy: false } : null));
    }
  }, []);

  useEffect(() => {
    const bridge = desktopBridge();
    if (!bridge?.isDesktop) return;
    setAvailable(true);
    void refresh();
    const interval = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(interval);
  }, [refresh]);

  const runRuntime = useCallback(async (action: "start" | "restart") => {
    const bridge = desktopBridge();
    if (!bridge) return;
    setBusy(true);
    try {
      setStatus(action === "start" ? await bridge.startRuntime() : await bridge.restartRuntime());
    } finally {
      setBusy(false);
    }
  }, []);

  const setComputerUse = useCallback(
    async (enabled: boolean) => {
      const bridge = desktopBridge();
      if (!bridge) return;
      setBusy(true);
      try {
        await bridge.setComputerUseEnabled(enabled);
        await refresh();
      } finally {
        setBusy(false);
      }
    },
    [refresh]
  );

  const openChromeExtensions = useCallback(() => void desktopBridge()?.openChromeExtensions(), []);
  const openDataFolder = useCallback(() => void desktopBridge()?.openDataFolder(), []);

  // The one problem worth interrupting for, in priority order. Computer use being off is a choice,
  // never a problem; a disconnected extension is normal (Chrome closed) and self-heals, since the
  // runtime opens Chrome when a task needs it. Only states the user must fix surface here.
  const problem: HostProblem | null = !status
    ? null
    : !status.backendHealthy
      ? {
          kind: "runtime",
          text: "Lumen's runtime is offline",
          detail: "Chat and tasks are unavailable until it starts.",
          action: { label: "Start runtime", run: () => void runRuntime("start") }
        }
      : !status.modelReady
        ? status.modelConfigured
          ? { kind: "model-unhealthy", text: "The model isn't responding", detail: status.modelError || "Check the provider in Settings → Models." }
          : { kind: "model-missing", text: "Connect a model to start", detail: "Lumen needs a model provider to think." }
        : !status.liveActionsReady && status.extensionConnected
          ? {
              kind: "extension",
              text: "Reload the Chrome extension",
              detail: "Chrome is running an older copy of Lumen's extension.",
              action: { label: "Open extensions", run: openChromeExtensions }
            }
          : null;

  return { available, status, busy, problem, refresh, runRuntime, setComputerUse, openChromeExtensions, openDataFolder };
}

export type DesktopStatusState = ReturnType<typeof useDesktopStatus>;
