import type { OperatorQuestion, OperatorRun } from "@/lib/api";

type DiagnosticLine = {
  label: string;
  value: string;
};

export type SystemBlockView = {
  reason: string;
  blockKind: string;
  needsUser: boolean;
  /** Short human title for the block state, e.g. "Lumen restarted during this task". */
  title: string;
  /** True when the block is a clean pause the user can resume (nothing was lost or
   * replayed), so the UI renders a calm resume affordance instead of an error card. */
  resumable: boolean;
  diagnostics: DiagnosticLine[];
};

type RunSurfaceView = {
  systemBlock: SystemBlockView | null;
  pendingQuestion: OperatorQuestion | null;
};

function asText(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function firstString(...values: unknown[]): string {
  for (const value of values) {
    const text = asText(value);
    if (text) return text;
  }
  return "";
}

function diagnosticLines(run: OperatorRun, systemBlock: Record<string, unknown> | null, endToEnd: Record<string, unknown> | null): DiagnosticLine[] {
  const lines: DiagnosticLine[] = [];
  const add = (label: string, value: unknown) => {
    const text = asText(value);
    if (text) lines.push({ label, value: text });
  };
  add("status_message", run.status_message);
  add("reason", firstString(systemBlock?.reason, endToEnd?.system_block_reason, run.confidence_state?.system_block_reason));
  add("original_reason", firstString(systemBlock?.original_reason, endToEnd?.original_reason));
  add("model_failure_reason", firstString(systemBlock?.model_failure_reason, endToEnd?.phase2_model_failure_reason, run.confidence_state?.phase2_model_failure_reason));
  add("block_kind", firstString(systemBlock?.block_kind, endToEnd?.block_kind, run.confidence_state?.block_kind));
  return lines;
}

const RUN_STOPPED_STATES = new Set(["BLOCKED", "WAITING_FOR_USER", "READY_FOR_REVIEW", "COMPLETED", "FAILED", "CANCELLED"]);

export function systemBlockFromRun(run: OperatorRun | null): SystemBlockView | null {
  if (!run) return null;
  // A system block explains why a run stopped. After an out-of-band resume the block
  // marker can linger in confidence_state until the next drive finalizes, so live
  // lifecycle state wins: an actively working run is never system-blocked.
  const currentState = String(run.current_state || "").toUpperCase();
  if (currentState && !RUN_STOPPED_STATES.has(currentState)) return null;
  const confidence = asRecord(run.confidence_state) || {};
  const rawSystemBlock = confidence.system_block;
  const systemBlock = asRecord(rawSystemBlock);
  const endToEnd = asRecord(confidence.end_to_end);
  const blockKind = firstString(systemBlock?.block_kind, endToEnd?.block_kind, confidence.block_kind);
  const isAwaitingEngineering = blockKind.toLowerCase() === "awaiting_engineering";
  const active = Boolean(systemBlock) || rawSystemBlock === true || endToEnd?.system_block === true || isAwaitingEngineering;
  if (!active) return null;

  const reason = firstString(systemBlock?.reason, endToEnd?.system_block_reason, confidence.system_block_reason, run.status_message);
  const rawNeedsUser = systemBlock?.needs_user ?? endToEnd?.needs_user;
  const needsUser = typeof rawNeedsUser === "boolean" ? rawNeedsUser : false;

  // A runtime restart is a clean, resumable pause: the reconciler superseded anything
  // pending and nothing was replayed. Rendering it as a generic runtime error teaches
  // users to ignore the error surface.
  // A screen that could not be captured (locked, asleep, a screensaver) is the user's to wake, and
  // the run picks up again from a fresh look.
  const kind = blockKind.toLowerCase();
  const resumable = kind === "runtime_restart" || kind === "screen_unavailable";
  const title =
    kind === "screen_unavailable"
      ? "Lumen couldn't see the screen"
      : resumable
        ? "Lumen restarted during this task"
        : "Lumen hit an internal problem";

  return {
    reason,
    blockKind,
    needsUser,
    title,
    resumable,
    diagnostics: diagnosticLines(run, systemBlock, endToEnd)
  };
}

const USER_ACTION_QUESTION_TYPES = new Set(["blocked_reason", "field_approval"]);

function pendingQuestionNeedsUser(question: OperatorQuestion | null, systemBlock: SystemBlockView | null): boolean {
  if (!question || question.status !== "pending") return false;
  if (systemBlock && systemBlock.needsUser === false) return false;
  if (USER_ACTION_QUESTION_TYPES.has(question.question_type)) return true;
  return Boolean(question.prompt?.trim());
}

export function buildRunSurfaceView(run: OperatorRun | null, pendingQuestion: OperatorQuestion | null): RunSurfaceView {
  const systemBlock = systemBlockFromRun(run);
  return {
    systemBlock,
    pendingQuestion: pendingQuestionNeedsUser(pendingQuestion, systemBlock) ? pendingQuestion : null
  };
}
