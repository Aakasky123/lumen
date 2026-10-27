"use client";

import { useState } from "react";
import { AlertTriangle, Check, ChevronDown, Copy, RotateCcw } from "lucide-react";
import {
  PendingQuestionCard,
  type ApplyArtifactFn,
  type FinalActionFn,
  type GenerateArtifactFn,
  type SelectFn,
  type UpdateArtifactFn,
  type UploadArtifactFn
} from "@/components/lumen/chat/PendingQuestionCard";
import { Button } from "@/components/ui/Button";
import { buildRunSurfaceView, type SystemBlockView } from "@/lib/runSurface";
import { api, type OperatorQuestion, type OperatorRun } from "@/lib/api";
import { cn } from "@/lib/utils";

function ResumableBlockCard({ block, run, statusInChat }: { block: SystemBlockView; run: OperatorRun | null; statusInChat: boolean }) {
  const [resuming, setResuming] = useState(false);
  const [resumeError, setResumeError] = useState("");
  const primary = run?.status_message?.trim() || block.reason || "Lumen paused and can pick this task back up.";

  const onResume = async () => {
    if (!run || resuming) return;
    setResuming(true);
    setResumeError("");
    try {
      await api.resume(run.id);
    } catch (error) {
      setResumeError(error instanceof Error ? error.message : "Couldn't resume. Try again.");
      setResuming(false);
      return;
    }
    // The run poller picks up the new state; keep the button quiet until it does.
  };

  return (
    <section className="animate-enter-up rounded-2xl border border-line-strong bg-surface-1 p-5 shadow-sm">
      <div className="flex items-start gap-3.5">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-xl border border-line bg-surface-2">
          <RotateCcw className={cn("size-4 text-fg-muted", resuming && "animate-spin")} />
        </span>
        <div className="min-w-0 flex-1">
          <h3 className="text-base font-semibold text-fg">{block.title}</h3>
          {statusInChat ? (
            <p className="mt-1 text-sm text-fg-muted">Resume picks it back up from a fresh look at the screen.</p>
          ) : (
            <p className="mt-1 whitespace-pre-wrap text-sm text-fg-muted [overflow-wrap:anywhere]">{primary}</p>
          )}
          <div className="mt-4 flex flex-wrap items-center gap-3">
            <Button variant="primary" size="sm" onClick={onResume} loading={resuming}>
              {!resuming ? <RotateCcw /> : null}
              {resuming ? "Resuming…" : "Resume task"}
            </Button>
            {resumeError ? <span className="text-13 text-danger-ink">{resumeError}</span> : null}
          </div>
        </div>
      </div>
    </section>
  );
}

function SystemBlockDiagnostic({ block, run, statusInChat }: { block: SystemBlockView; run: OperatorRun | null; statusInChat: boolean }) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const primary = run?.status_message?.trim() || block.reason || "The browser agent hit an internal runtime blocker.";
  const diagnostics = block.diagnostics.length
    ? block.diagnostics
    : [{ label: "reason", value: primary }, ...(block.blockKind ? [{ label: "block_kind", value: block.blockKind }] : [])];

  return (
    <section className="animate-enter-up overflow-hidden rounded-2xl border border-danger/25 bg-surface-1 shadow-sm">
      <div className="flex items-start gap-3.5 p-5">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-xl border border-danger/25 bg-danger/10">
          <AlertTriangle className="size-4 text-danger-ink" />
        </span>
        <div className="min-w-0 flex-1">
          <h3 className="text-base font-semibold text-fg">{block.title}</h3>
          {statusInChat ? null : <p className="mt-1 whitespace-pre-wrap text-sm text-fg-muted [overflow-wrap:anywhere]">{primary}</p>}
          <p className={cn("text-13", statusInChat ? "mt-1 text-fg-muted" : "mt-2 text-fg-subtle")}>Nothing more will happen on the page. You can ask Lumen to try again, or give it a new task.</p>
        </div>
      </div>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full items-center gap-1.5 border-t border-line px-5 py-2.5 text-xs font-medium text-fg-subtle transition hover:bg-fg/[0.03] hover:text-fg focus-ring-inset"
      >
        Technical details
        <ChevronDown className={cn("size-3.5 transition-transform", open && "rotate-180")} />
      </button>
      {open ? (
        <dl className="animate-enter-fade border-t border-line bg-fg/[0.02]">
          {diagnostics.map((line) => (
            <div key={`${line.label}-${line.value}`} className="grid gap-1 border-b border-line px-5 py-2 text-xs sm:grid-cols-[11rem_minmax(0,1fr)]">
              <dt className="font-medium text-fg-subtle">{line.label}</dt>
              <dd className="min-w-0 break-words font-mono text-fg-muted">{line.value}</dd>
            </div>
          ))}
          <div className="flex justify-end px-5 py-2">
            <button
              type="button"
              onClick={() => {
                // One line per detail, with the run, ready to paste into a report.
                const text = [...(run ? [`run: ${run.id}`] : []), ...diagnostics.map((line) => `${line.label}: ${line.value}`)].join("\n");
                void navigator.clipboard?.writeText(text).then(() => {
                  setCopied(true);
                  window.setTimeout(() => setCopied(false), 1400);
                });
              }}
              className="flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium text-fg-subtle transition hover:bg-fg/[0.06] hover:text-fg focus-ring"
            >
              {copied ? <Check className="size-3.5 text-brand-ink" /> : <Copy className="size-3.5" />}
              {copied ? "Copied" : "Copy details"}
            </button>
          </div>
        </dl>
      ) : null}
    </section>
  );
}

export function RunTimelineBlock({
  runId,
  run,
  pendingQuestion,
  sending,
  onSelect,
  onFinalAction,
  onUploadArtifact,
  onGenerateArtifact,
  onUpdateArtifact,
  onApplyArtifact,
  statusInChat = false
}: {
  runId: number | null;
  run: OperatorRun | null;
  pendingQuestion: OperatorQuestion | null;
  sending: boolean;
  onSelect: SelectFn;
  onFinalAction: FinalActionFn;
  onUploadArtifact: UploadArtifactFn;
  onGenerateArtifact: GenerateArtifactFn;
  onUpdateArtifact: UpdateArtifactFn;
  onApplyArtifact: ApplyArtifactFn;
  /** The run's status line is already the latest chat message, so block cards needn't repeat it. */
  statusInChat?: boolean;
}) {
  const view = buildRunSurfaceView(run, pendingQuestion);
  // Only things that need the user render here: blockers and questions. The run dock narrates
  // live progress and the chat messages carry the outcome.
  if (!runId || !(view.systemBlock || view.pendingQuestion)) return null;

  return (
    <div className="space-y-3">
      {view.systemBlock ? (
        view.systemBlock.resumable ? (
          <ResumableBlockCard block={view.systemBlock} run={run} statusInChat={statusInChat} />
        ) : (
          <SystemBlockDiagnostic block={view.systemBlock} run={run} statusInChat={statusInChat} />
        )
      ) : null}
      {view.pendingQuestion ? (
        <PendingQuestionCard
          key={view.pendingQuestion.id}
          question={view.pendingQuestion}
          onSelect={onSelect}
          onFinalAction={onFinalAction}
          onUploadArtifact={onUploadArtifact}
          onGenerateArtifact={onGenerateArtifact}
          onUpdateArtifact={onUpdateArtifact}
          onApplyArtifact={onApplyArtifact}
          sending={sending}
        />
      ) : null}
    </div>
  );
}
