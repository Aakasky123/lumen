"use client";

import { useEffect, useState, type ReactNode } from "react";
import { ArrowRight, Briefcase, ClipboardList, Cpu, Globe, Monitor, Pause } from "lucide-react";
import type { ChatThread, OperatorRun } from "@/lib/api";
import { formatRelative } from "@/lib/format";
import { PHASE_META, runPhase, runTargetLabel } from "@/lib/runPhase";
import { useIsMac } from "@/lib/platform";
import { threadTitle } from "@/lib/threads";
import { LumenMark } from "@/components/lumen/brand/LumenMark";
import { Button } from "@/components/ui/Button";
import { StatusDot } from "@/components/ui/badge";
import { cn, parseBackendDate } from "@/lib/utils";

type Suggestion = { icon: typeof Globe; title: string; detail: string; prompt: string; select?: string };

// Suggestions fill the composer instead of sending: Lumen acts on your machine, so a task starts
// only when you have read it and pressed Enter.
const SUGGESTIONS: Suggestion[] = [
  {
    icon: Globe,
    title: "Research and compare",
    detail: "Read reviews and prices, then summarize",
    prompt: "Compare the three best-reviewed standing desks under $400 and summarize the trade-offs."
  },
  {
    icon: ClipboardList,
    title: "Fill out a form",
    detail: "From your profile, stopping before submit",
    prompt: "Fill out the form at [paste the link] with my profile, and stop before submitting.",
    select: "[paste the link]"
  },
  {
    icon: Briefcase,
    title: "Apply to a job",
    detail: "With your résumé and profile",
    prompt: "Apply to this job with my résumé: [paste the job link]",
    select: "[paste the job link]"
  },
  {
    icon: Monitor,
    title: "Work in a Windows app",
    detail: "Files, settings, and desktop apps",
    prompt: "In File Explorer, sort my Downloads folder into subfolders by file type."
  }
];

// On a Mac the desktop card names its file manager.
const MAC_DESKTOP_SUGGESTION: Suggestion = {
  icon: Monitor,
  title: "Work in a Mac app",
  detail: "Files, settings, and desktop apps",
  prompt: "In Finder, sort my Downloads folder into subfolders by file type."
};

function greeting(hour: number): string {
  if (hour < 5) return "Working late";
  if (hour < 12) return "Good morning";
  if (hour < 17) return "Good afternoon";
  return "Good evening";
}

export type WaitingItem = { thread: ChatThread; run: OperatorRun };

export function HomeScreen({
  firstName,
  composer,
  onSuggestion,
  waiting,
  waitingDisabled = false,
  onOpenThread,
  needsModel,
  onOpenModels,
  notice
}: {
  firstName: string;
  composer: ReactNode;
  onSuggestion: (prompt: string, select?: string) => void;
  waiting: WaitingItem[];
  /** True while chat is locked (connecting, or a secure entry is in progress). */
  waitingDisabled?: boolean;
  onOpenThread: (thread: ChatThread) => void;
  needsModel: boolean;
  onOpenModels: () => void;
  /** A host problem worth fixing before starting (runtime offline, extension stale). */
  notice?: ReactNode;
}) {
  // Time-dependent copy renders after mount: the cockpit is prerendered at build time.
  const [hello, setHello] = useState("");
  useEffect(() => setHello(greeting(new Date().getHours())), []);
  const isMac = useIsMac();

  return (
    <div className="scroll-area min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex min-h-full w-full max-w-[44rem] flex-col justify-center px-6 pb-[12vh] pt-10">
        <div className="mb-8 animate-enter-up">
          <span className="mb-6 flex size-11 items-center justify-center rounded-2xl border border-line-strong bg-surface-1 shadow-md">
            <LumenMark className="size-6" />
          </span>
          <p className={cn("h-6 text-15 text-fg-muted transition-opacity duration-500", hello ? "opacity-100" : "opacity-0")}>
            {hello ? `${hello}${firstName ? `, ${firstName}` : ""}.` : " "}
          </p>
          <h1 className="mt-1 text-3xl font-semibold text-fg">What should I work on?</h1>
        </div>

        {notice ? <div className="mb-4 animate-enter-up">{notice}</div> : null}

        {needsModel ? (
          <div className="mb-4 flex animate-enter-up items-start gap-4 rounded-2xl border border-brand/30 bg-brand/[0.05] p-5">
            <span className="flex size-10 shrink-0 items-center justify-center rounded-xl border border-brand/30 bg-brand/10 text-brand-ink">
              <Cpu className="size-5" />
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-15 font-semibold text-fg">Connect a model to get started</p>
              <p className="mt-1 text-13 text-fg-muted">
                Lumen thinks with a model you choose: OpenAI, Anthropic, Gemini, OpenRouter, DeepSeek, or any OpenAI-compatible endpoint. Keys are stored encrypted on this computer.
              </p>
              <Button variant="primary" size="sm" className="mt-3.5" onClick={onOpenModels}>
                Add a model <ArrowRight />
              </Button>
            </div>
          </div>
        ) : null}

        <div className="animate-enter-up [animation-delay:60ms]">{composer}</div>

        <div className="mt-4 grid animate-enter-up grid-cols-1 gap-2 [animation-delay:120ms] sm:grid-cols-2">
          {(isMac ? SUGGESTIONS.map((item) => (item.icon === Monitor ? MAC_DESKTOP_SUGGESTION : item)) : SUGGESTIONS).map((suggestion) => (
            <button
              key={suggestion.title}
              type="button"
              onClick={() => onSuggestion(suggestion.prompt, suggestion.select)}
              className="group/suggest flex items-start gap-3 rounded-xl border border-line bg-surface-1/60 px-3.5 py-3 text-left transition hover:border-line-strong hover:bg-surface-1 focus-ring"
            >
              <suggestion.icon className="mt-0.5 size-4 shrink-0 text-fg-subtle transition group-hover/suggest:text-brand-ink" />
              <span className="min-w-0">
                <span className="block text-13 font-medium text-fg">{suggestion.title}</span>
                <span className="block text-xs text-fg-subtle">{suggestion.detail}</span>
              </span>
            </button>
          ))}
        </div>

        {waiting.length ? (
          <section className="mt-10 animate-enter-up [animation-delay:180ms]" aria-label="Waiting on you">
            <p className="eyebrow mb-2 px-1">Waiting on you</p>
            <ul className="overflow-hidden rounded-xl border border-line bg-surface-1">
              {waiting.slice(0, 4).map(({ thread, run }) => {
                const phase = runPhase(run);
                const updated = parseBackendDate(run.updated_at);
                return (
                  <li key={thread.id} className="border-b border-line last:border-b-0">
                    <button
                      type="button"
                      disabled={waitingDisabled}
                      onClick={() => onOpenThread(thread)}
                      className="group/wait flex w-full items-center gap-3 px-4 py-3 text-left transition hover:bg-fg/[0.03] focus-ring-inset disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {phase === "paused" ? <Pause className="size-3.5 shrink-0 text-fg-subtle" /> : <StatusDot tone="attention" pulse={phase === "needs_you"} />}
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-13 font-medium text-fg">{threadTitle(thread, run)}</span>
                        <span className="block truncate text-xs text-fg-subtle">
                          {phase ? PHASE_META[phase].label : ""}
                          {runTargetLabel(run.target_url) ? ` · ${runTargetLabel(run.target_url)}` : ""}
                          {Number.isNaN(updated.getTime()) ? "" : ` · ${formatRelative(updated)}`}
                        </span>
                      </span>
                      <ArrowRight className="size-4 shrink-0 text-fg-subtle transition group-hover/wait:translate-x-0.5 group-hover/wait:text-fg" />
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>
        ) : null}
      </div>
    </div>
  );
}
