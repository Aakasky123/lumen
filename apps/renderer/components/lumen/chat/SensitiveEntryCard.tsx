"use client";

import { ArrowRight, LockKeyhole } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { runTargetLabel } from "@/lib/runPhase";

/**
 * A run that needs a secret (a password, a one-time code) hands entry to the user, in Chrome or in
 * the app window on the desktop. The value never passes through chat or a model; this card's
 * Continue is text-free.
 */
export function SensitiveEntryCard({
  targetUrl,
  runId,
  entrySurface,
  busy,
  canContinue,
  onContinue,
  onCancel
}: {
  targetUrl: string;
  runId: number;
  entrySurface: "browser" | "desktop";
  busy: boolean;
  canContinue: boolean;
  onContinue: () => void;
  onCancel: () => void;
}) {
  const desktop = entrySurface === "desktop";
  const site = runTargetLabel(targetUrl) || `task #${runId}`;
  const steps = desktop
    ? ["Switch to the app window Lumen is working in.", "Enter the value directly in that window.", "Come back here and press Continue."]
    : ["Switch to the Chrome tab Lumen is working in.", "Enter the value directly on the page.", "Come back here and press Continue."];
  return (
    <section className="animate-enter-up overflow-hidden rounded-2xl border border-brand/30 bg-surface-1 shadow-md" aria-label="Secure entry">
      <header className="flex items-center gap-2 border-b border-brand/15 bg-brand/[0.06] px-4 py-2 text-brand-ink">
        <LockKeyhole className="size-4" />
        <span className="text-13 font-semibold">Secure entry</span>
        <span className="ml-auto text-2xs font-medium text-fg-subtle">Chat is paused</span>
      </header>
      <div className="px-5 pb-5 pt-4">
        <h3 className="text-base font-semibold text-fg">{desktop ? "Type it into the app yourself" : "Type it into Chrome yourself"}</h3>
        <p className="mt-1 text-sm text-fg-muted">
          Passwords and one-time codes never go through chat or a model. Lumen waits while you enter this one{" "}
          {desktop ? (
            "in the app window on your screen."
          ) : (
            <>
              on <span className="font-medium text-fg">{site}</span>.
            </>
          )}
        </p>
        <ol className="mt-4 space-y-2.5">
          {steps.map((step, index) => (
            <li key={step} className="flex items-start gap-3 text-sm text-fg">
              <span className="flex size-5 shrink-0 items-center justify-center rounded-full border border-line-strong text-2xs font-semibold text-fg-muted tabular">{index + 1}</span>
              {step}
            </li>
          ))}
        </ol>
        <div className="mt-5 flex flex-wrap items-center gap-2">
          <Button variant="primary" size="md" loading={busy} disabled={!canContinue} onClick={onContinue}>
            I&rsquo;ve entered it — continue
            {!busy ? <ArrowRight /> : null}
          </Button>
          <Button variant="danger-ghost" size="md" disabled={busy} onClick={onCancel}>
            Cancel task
          </Button>
        </div>
        {!canContinue ? (
          <p className="mt-3 text-13 text-danger-ink">This hand-off lost its secure link, so it can&rsquo;t continue. Cancel the task to unlock chat.</p>
        ) : null}
      </div>
    </section>
  );
}
