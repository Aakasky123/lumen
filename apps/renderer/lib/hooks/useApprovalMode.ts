"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type AgentSettings } from "@/lib/api";

export type ApprovalMode = AgentSettings["final_action_mode"];

const APPROVAL_MODE_EVENT = "lumen:approval-mode-changed";

function publishApprovalMode(mode: ApprovalMode) {
  window.dispatchEvent(new CustomEvent(APPROVAL_MODE_EVENT, { detail: { mode } }));
}

export function useApprovalMode(load: boolean) {
  const [mode, setMode] = useState<ApprovalMode>("approval");
  const [loading, setLoading] = useState(load);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await api.agentSettings();
      const next = response.settings.final_action_mode ?? "approval";
      setMode(next);
      publishApprovalMode(next);
    } catch {
      setError("Could not load Lumen's approval setting.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const onModeChanged = (event: Event) => {
      const detail = (event as CustomEvent<{ mode?: ApprovalMode }>).detail;
      if (detail?.mode === "approval" || detail?.mode === "autonomous") {
        setMode(detail.mode);
      }
    };
    window.addEventListener(APPROVAL_MODE_EVENT, onModeChanged);
    if (load) void refresh();
    return () => window.removeEventListener(APPROVAL_MODE_EVENT, onModeChanged);
  }, [load, refresh]);

  const update = useCallback(
    async (next: ApprovalMode): Promise<boolean> => {
      if (next === mode) return true;
      const previous = mode;
      setMode(next);
      setSaving(true);
      setError(null);
      publishApprovalMode(next);
      try {
        const response = await api.updateAgentSettings({ final_action_mode: next });
        const persisted = response.settings.final_action_mode ?? "approval";
        setMode(persisted);
        publishApprovalMode(persisted);
        return persisted === next;
      } catch {
        setMode(previous);
        publishApprovalMode(previous);
        setError("Could not save Lumen's approval setting.");
        return false;
      } finally {
        setSaving(false);
      }
    },
    [mode]
  );

  return { mode, loading, saving, error, update };
}
