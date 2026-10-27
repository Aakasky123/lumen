"use client";

import { memo, useState } from "react";
import { AlertCircle, Check, CheckCircle2, CircleSlash, Copy, Eye, OctagonPause, RotateCcw, XCircle } from "lucide-react";
import type { ChatMessage as ChatMessageType } from "@/lib/api";
import { ChatMarkdown } from "@/components/lumen/chat/ChatMarkdown";
import { LumenMark } from "@/components/lumen/brand/LumenMark";
import { Button } from "@/components/ui/Button";
import { Tooltip } from "@/components/ui/tooltip";
import { cn, parseBackendDate } from "@/lib/utils";

function clientStatus(message: ChatMessageType): string | null {
  const status = message.payload_json?.client_status;
  return typeof status === "string" ? status : null;
}

function timeOf(message: ChatMessageType): string {
  const date = parseBackendDate(message.created_at);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <Tooltip label={copied ? "Copied" : "Copy"} side="bottom">
      <button
        type="button"
        aria-label="Copy message"
        onClick={() => {
          void navigator.clipboard?.writeText(text).then(() => {
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1400);
          });
        }}
        className="flex size-7 items-center justify-center rounded-md text-fg-subtle transition hover:bg-fg/[0.06] hover:text-fg focus-ring"
      >
        {copied ? <Check className="size-3.5 text-brand-ink" /> : <Copy className="size-3.5" />}
      </button>
    </Tooltip>
  );
}

function MessageMeta({ message, align = "start" }: { message: ChatMessageType; align?: "start" | "end" }) {
  const time = timeOf(message);
  return (
    <div
      className={cn(
        "flex h-7 items-center gap-1 opacity-0 transition-opacity duration-150 group-hover:opacity-100 group-focus-within:opacity-100",
        align === "end" ? "justify-end" : "justify-start -ml-1.5"
      )}
    >
      {align === "end" && time ? <span className="px-1 text-2xs text-fg-subtle tabular">{time}</span> : null}
      <CopyButton text={message.content} />
      {align === "start" && time ? <span className="px-1 text-2xs text-fg-subtle tabular">{time}</span> : null}
    </div>
  );
}

export function AssistantAvatar({ working = false }: { working?: boolean }) {
  return (
    <span
      className={cn(
        "mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-lg border border-line bg-surface-1 shadow-xs transition-shadow duration-500",
        working && "shadow-brand"
      )}
    >
      <LumenMark className="size-[15px]" working={working} />
    </span>
  );
}

type OutcomeState = "COMPLETED" | "READY_FOR_REVIEW" | "BLOCKED" | "FAILED" | "CANCELLED" | "WAITING_FOR_USER";

const OUTCOME: Record<OutcomeState, { label: string; icon: typeof CheckCircle2; tone: string; ring: string }> = {
  COMPLETED: { label: "Done", icon: CheckCircle2, tone: "text-brand-ink", ring: "border-brand/30" },
  // A hand-off, not a success: the page waits for you and nothing final was done.
  READY_FOR_REVIEW: { label: "Ready for your review", icon: Eye, tone: "text-fg", ring: "border-line-strong" },
  BLOCKED: { label: "Stopped", icon: OctagonPause, tone: "text-attention-ink", ring: "border-attention/30" },
  FAILED: { label: "Couldn't finish", icon: XCircle, tone: "text-danger-ink", ring: "border-danger/30" },
  CANCELLED: { label: "Ended", icon: CircleSlash, tone: "text-fg-muted", ring: "border-line-strong" },
  WAITING_FOR_USER: { label: "Waiting for you", icon: OctagonPause, tone: "text-attention-ink", ring: "border-attention/30" }
};

type Receipt = { outcome?: unknown };
type CensusEntry = { status?: unknown };

/**
 * A run outcome the backend posted: its state and the receipt counts, tallied the way the backend's
 * own outcome message tallies them (verified, inconclusive, and failed = execution or verification
 * failed), plus the required fields its field census still lists as open.
 */
function outcomeOf(
  message: ChatMessageType
): { state: OutcomeState; verified: number; unconfirmed: number; failed: number; requiredOpen: number; completion: string } | null {
  if (message.payload_json?.source !== "in_browser_outcome") return null;
  const summary = message.payload_json?.summary as
    | { state?: unknown; receipts?: unknown; field_census?: unknown; completion_check?: { verdict?: unknown } }
    | undefined;
  const state = String(summary?.state || "").toUpperCase() as OutcomeState;
  if (!(state in OUTCOME)) return null;
  const receipts = Array.isArray(summary?.receipts) ? (summary?.receipts as Receipt[]) : [];
  const census = Array.isArray(summary?.field_census) ? (summary?.field_census as CensusEntry[]) : [];
  return {
    state,
    verified: receipts.filter((receipt) => receipt?.outcome === "verified_succeeded").length,
    unconfirmed: receipts.filter((receipt) => receipt?.outcome === "verification_inconclusive").length,
    failed: receipts.filter((receipt) => receipt?.outcome === "execution_failed" || receipt?.outcome === "verification_failed").length,
    requiredOpen: census.filter((entry) => entry?.status === "required_unfilled").length,
    // The judge model's check of a generic task's "done" against the screen (complete, incomplete, unverified).
    completion: String(summary?.completion_check?.verdict || "")
  };
}

function OutcomeMessage({ message, outcome }: { message: ChatMessageType; outcome: NonNullable<ReturnType<typeof outcomeOf>> }) {
  // Required fields still open means the hand-off needs you, whatever the run state says.
  // A generic task's hand-off says whether the screen backed the finish: a confirmed finish reads as
  // finished, an unbacked one asks you to look, never as a success.
  const review = outcome.state === "READY_FOR_REVIEW";
  const meta =
    review && outcome.requiredOpen
      ? { label: "Needs you", icon: OctagonPause, tone: "text-attention-ink", ring: "border-attention/30" }
      : review && outcome.completion === "complete"
        ? { label: "Finished", icon: CheckCircle2, tone: "text-brand-ink", ring: "border-brand/30" }
        : review && (outcome.completion === "incomplete" || outcome.completion === "unverified")
          ? { label: "Not confirmed", icon: AlertCircle, tone: "text-attention-ink", ring: "border-attention/30" }
          : OUTCOME[outcome.state];
  const Icon = meta.icon;
  return (
    <div className="group flex gap-3.5">
      <AssistantAvatar />
      <div className="min-w-0 flex-1">
        <div className={cn("overflow-hidden rounded-xl border bg-surface-1 shadow-sm", meta.ring)}>
          <div className="flex items-center gap-2 border-b border-line px-4 py-2.5">
            <Icon className={cn("size-4", meta.tone)} />
            <span className={cn("text-13 font-semibold", meta.tone)}>{meta.label}</span>
            <span className="ml-auto flex items-center gap-3 text-xs text-fg-subtle">
              {outcome.verified ? (
                <span className="flex items-center gap-1 tabular" title="Steps whose result Lumen read back and confirmed">
                  <Check className="size-3.5 text-brand-ink" />
                  {outcome.verified} verified {outcome.verified === 1 ? "step" : "steps"}
                </span>
              ) : null}
              {outcome.unconfirmed ? (
                <span className="tabular text-attention-ink" title="Steps Lumen ran but could not confirm">
                  {outcome.unconfirmed} unconfirmed
                </span>
              ) : null}
              {outcome.failed ? (
                <span className="tabular text-danger-ink" title="Steps that did not run, or whose result read back wrong">
                  {outcome.failed} failed
                </span>
              ) : null}
              {outcome.requiredOpen ? (
                <span className="tabular text-attention-ink" title="Required fields still empty on the page">
                  {outcome.requiredOpen} required open
                </span>
              ) : null}
            </span>
          </div>
          <div className="px-4 py-3.5">
            <ChatMarkdown content={message.content} />
          </div>
        </div>
        <MessageMeta message={message} />
      </div>
    </div>
  );
}

export const ChatMessage = memo(function ChatMessage({ message, onRetry }: { message: ChatMessageType; onRetry: (message: ChatMessageType) => void }) {
  const status = clientStatus(message);
  const thinking = status === "loading" && !message.content.trim();

  if (message.role === "user") {
    return (
      <div className="group flex animate-enter-up flex-col items-end">
        <div className="max-w-[min(85%,36rem)] whitespace-pre-wrap rounded-[20px] rounded-br-md border border-line bg-surface-2 px-4 py-2.5 text-15 text-fg shadow-xs [overflow-wrap:anywhere]">
          {message.content}
        </div>
        {status === "sending" ? <div className="h-7" /> : <MessageMeta message={message} align="end" />}
      </div>
    );
  }

  const outcome = outcomeOf(message);
  if (outcome && !status) return <OutcomeMessage message={message} outcome={outcome} />;

  if (thinking) {
    return (
      <div className="flex animate-enter-fade gap-3.5" role="status" aria-label="Lumen is thinking">
        <AssistantAvatar working />
        <span className="text-shimmer pt-1 text-15 font-medium">Thinking</span>
      </div>
    );
  }

  if (status === "error") {
    return (
      <div className="flex animate-enter-up gap-3.5">
        <AssistantAvatar />
        <div role="alert" className="flex min-w-0 flex-1 items-start gap-3 rounded-xl border border-danger/30 bg-danger/[0.06] px-4 py-3">
          <AlertCircle className="mt-0.5 size-4 shrink-0 text-danger-ink" />
          <div className="min-w-0 flex-1">
            <p className="text-sm text-fg">{message.content}</p>
            <Button variant="ghost" size="xs" className="-ml-2.5 mt-1.5 text-fg" onClick={() => onRetry(message)}>
              <RotateCcw /> Try again
            </Button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="group flex gap-3.5">
      <AssistantAvatar working={status === "loading"} />
      <div className="min-w-0 flex-1 pt-[3px]">
        <ChatMarkdown content={message.content} streaming={status === "loading"} />
        {status === "loading" ? <div className="h-7" /> : <MessageMeta message={message} />}
      </div>
    </div>
  );
});
