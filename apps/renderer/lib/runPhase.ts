import type { OperatorRun } from "@/lib/api";

/**
 * One reading of a run's lifecycle for every surface (run dock, chat list, home screen). Derived
 * only from backend state; the cockpit never invents progress.
 */
export type RunPhase = "working" | "paused" | "needs_you" | "review" | "stopped" | "done" | "failed" | "ended";

export type PhaseTone = "brand" | "attention" | "danger" | "neutral";

export const PHASE_META: Record<RunPhase, { label: string; tone: PhaseTone; live: boolean }> = {
  working: { label: "Working", tone: "brand", live: true },
  paused: { label: "Paused", tone: "neutral", live: true },
  needs_you: { label: "Needs you", tone: "attention", live: true },
  review: { label: "Ready for review", tone: "brand", live: false },
  stopped: { label: "Stopped", tone: "neutral", live: false },
  done: { label: "Done", tone: "brand", live: false },
  failed: { label: "Couldn't finish", tone: "danger", live: false },
  ended: { label: "Ended", tone: "neutral", live: false }
};

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

export function runIsPaused(run: Pick<OperatorRun, "confidence_state">): boolean {
  const confidence = run.confidence_state || {};
  const userPause = record(confidence.user_pause);
  const sessionControl = record(confidence.session_control);
  return Boolean(userPause?.active) || String(sessionControl?.status || "").toLowerCase() === "paused";
}

export function runHasPendingBrowserResumeRetry(run: Pick<OperatorRun, "confidence_state">): boolean {
  return Boolean(record(run.confidence_state?.in_browser_resume_retry)?.pending);
}

export function runPhase(run: Pick<OperatorRun, "current_state" | "confidence_state"> | null | undefined): RunPhase | null {
  if (!run) return null;
  const state = String(run.current_state || "").toUpperCase();
  if (state === "CANCELLED") return "ended";
  if (state === "FAILED") return "failed";
  if (state === "COMPLETED") return "done";
  if (state === "READY_FOR_REVIEW") return "review";
  if (state === "BLOCKED") return "stopped";
  if (runIsPaused(run)) return "paused";
  if (state === "WAITING_FOR_USER") return runHasPendingBrowserResumeRetry(run) ? "paused" : "needs_you";
  return "working";
}

function cockpitOnMac(): boolean {
  return typeof navigator !== "undefined" && /mac/i.test(navigator.platform || navigator.userAgent || "");
}

/** Where a run works, in words: a site's host, or the desktop's name ("Windows", "Mac") for the desktop surface. */
export function runTargetLabel(targetUrl: string | null | undefined, onMac: boolean = cockpitOnMac()): string {
  const raw = String(targetUrl || "").trim();
  if (!raw) return "";
  // Runs record the desktop surface as one marker on either OS; the cockpit's own OS names it.
  if (raw.startsWith("desktop://")) return onMac ? "Mac" : "Windows";
  try {
    return new URL(raw).hostname.replace(/^www\./, "");
  } catch {
    return raw;
  }
}

export type ContextUsage = { usedTokens: number; limitTokens: number | null; model: string; source: string };

export function runContextUsage(run: Pick<OperatorRun, "confidence_state"> | null | undefined): ContextUsage | null {
  const usage = record(run?.confidence_state?.context_usage);
  if (!usage) return null;
  const used = Number(usage.used_tokens || 0);
  if (!Number.isFinite(used) || used <= 0) return null;
  const limit = Number(usage.limit_tokens || 0);
  return {
    usedTokens: used,
    limitTokens: Number.isFinite(limit) && limit > 0 ? limit : null,
    model: String(usage.model || usage.provider_display_name || ""),
    source: String(usage.context_window_source || "")
  };
}
