"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type OperatorRun } from "@/lib/api";

export type ActiveRun = ReturnType<typeof useActiveRun>;

export function useActiveRun(runId: number | null) {
  const [run, setRun] = useState<OperatorRun | null>(null);
  const [busy, setBusy] = useState(false);
  const [controlError, setControlError] = useState<string | null>(null);
  const runIdRef = useRef<number | null>(runId);
  const busyRef = useRef(false);
  runIdRef.current = runId;

  const refresh = useCallback(async () => {
    const id = runIdRef.current;
    if (!id) {
      setRun(null);
      setControlError(null);
      return;
    }
    try {
      setRun(await api.getRun(id));
    } catch {
      // keep last known run across transient polling errors
    }
  }, []);

  useEffect(() => {
    if (!runId) {
      setRun(null);
      return;
    }
    let active = true;
    void refresh();
    const interval = window.setInterval(() => {
      if (active) void refresh();
    }, 3000);
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, [runId, refresh]);

  const control = useCallback(async (
    actionName: string,
    action: (id: number) => Promise<OperatorRun>
  ) => {
    const id = runIdRef.current;
    if (!id || busyRef.current) return null;
    busyRef.current = true;
    setBusy(true);
    setControlError(null);
    try {
      const next = await action(id);
      setRun(next);
      return next;
    } catch (error) {
      const detail = error instanceof Error ? error.message.trim() : "";
      const runtimeUnavailable = /failed to fetch|networkerror|load failed/i.test(detail);
      setControlError(
        runtimeUnavailable
          ? `Could not ${actionName}. Lumen's runtime is unavailable. Start the runtime and try again.`
          : `Could not ${actionName}. ${detail || "Please try again."}`
      );
      return null;
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }, []);

  const pause = useCallback(() => control("pause this task", api.pause), [control]);
  const resume = useCallback(() => control("resume this task", api.resume), [control]);
  const continueSensitiveBrowser = useCallback(
    (handoffId: string) => control(
      "continue this task",
      (id) => api.sensitiveBrowserContinue(id, handoffId)
    ),
    [control]
  );
  const end = useCallback(() => control("end this task", api.cancel), [control]);
  return { run, busy, controlError, refresh, pause, resume, continueSensitiveBrowser, end };
}
