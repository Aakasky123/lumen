import type { OperatorRun } from "@/lib/api";
import { runHasPendingBrowserResumeRetry, runIsPaused } from "@/lib/runPhase";

type RunStatus = {
  canPause: boolean;
  canResume: boolean;
  canEnd: boolean;
};

const INERT: RunStatus = { canPause: false, canResume: false, canEnd: false };
const END_ONLY: RunStatus = { canPause: false, canResume: false, canEnd: true };
const RESUMABLE: RunStatus = { canPause: false, canResume: true, canEnd: true };
const LIVE: RunStatus = { canPause: true, canResume: false, canEnd: true };

export function runStatus(run: OperatorRun | null): RunStatus {
  if (!run) return INERT;
  const state = String(run.current_state || "").toUpperCase();
  if (state === "CANCELLED" || state === "FAILED" || state === "COMPLETED") return INERT;
  if (state === "READY_FOR_REVIEW" || state === "BLOCKED") return END_ONLY;
  if (runIsPaused(run)) return RESUMABLE;
  if (state === "WAITING_FOR_USER") return runHasPendingBrowserResumeRetry(run) ? RESUMABLE : END_ONLY;
  return LIVE;
}

/**
 * A run is live while something is in motion or holding the browser: it can be paused or resumed,
 * or it is waiting on you. Only a live run offers Stop; a blocked or reviewed run has nothing to
 * stop, and a new task supersedes it server-side.
 */
export function runIsLive(run: OperatorRun | null): boolean {
  if (!run) return false;
  const status = runStatus(run);
  const state = String(run.current_state || "").toUpperCase();
  return status.canPause || status.canResume || state.startsWith("WAITING");
}

export function formatTokens(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k`;
  return String(n);
}

export function formatBytes(size: number): string {
  if (!size) return "0 B";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatElapsed(totalSeconds: number): string {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  if (hours) return `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

/** "just now", "5m ago", "3h ago", "Yesterday", or a short date. */
export function formatRelative(date: Date, now: Date = new Date()): string {
  const seconds = (now.getTime() - date.getTime()) / 1000;
  if (!Number.isFinite(seconds)) return "";
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  if (date.getTime() >= startOfToday) return `${Math.floor(seconds / 3600)}h ago`;
  if (date.getTime() >= startOfToday - 86_400_000) return "Yesterday";
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

/**
 * Whether a file's name ends in one of an `accept` list's extensions (".pdf,.docx"). A drop bypasses
 * the file picker's own filter, so drop targets check this before uploading.
 */
export function fileMatchesAccept(file: { name: string }, accept: string): boolean {
  const allowed = accept
    .split(",")
    .map((ext) => ext.trim().toLowerCase())
    .filter((ext) => ext.startsWith("."));
  if (!allowed.length) return true;
  const name = file.name.toLowerCase();
  return allowed.some((ext) => name.endsWith(ext));
}
