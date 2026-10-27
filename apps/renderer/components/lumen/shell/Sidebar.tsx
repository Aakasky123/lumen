"use client";

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { FileText, KeyRound, PanelLeftClose, PanelLeftOpen, Pause, Search, Settings, SquarePen, Trash2, UserRound } from "lucide-react";
import type { ChatThread, OperatorRun } from "@/lib/api";
import { formatCostUsd, threadCostUsd } from "@/lib/cost";
import { PHASE_META, runPhase, type RunPhase } from "@/lib/runPhase";
import { groupByRecency, threadTitle } from "@/lib/threads";
import { LumenMark, LumenWordmark } from "@/components/lumen/brand/LumenMark";
import { Button } from "@/components/ui/Button";
import { Kbd, StatusDot } from "@/components/ui/badge";
import { Tooltip } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { useIsMac } from "@/lib/platform";

export type PanelKey = "profile" | "documents" | "signins" | "settings";

const PANELS: Array<{ key: PanelKey; label: string; icon: typeof UserRound; shortcut?: string[] }> = [
  { key: "profile", label: "Profile", icon: UserRound },
  { key: "documents", label: "Documents", icon: FileText },
  { key: "signins", label: "Sign-ins", icon: KeyRound },
  { key: "settings", label: "Settings", icon: Settings, shortcut: ["Ctrl", ","] }
];

function PhaseIndicator({ phase }: { phase: RunPhase | null }) {
  if (!phase) return null;
  const label = PHASE_META[phase].label;
  if (phase === "working") return <LumenMark className="size-3.5" working title={label} />;
  if (phase === "needs_you") return <span title={label}><StatusDot tone="attention" pulse /></span>;
  if (phase === "paused") return <span title={label}><Pause className="size-3 text-fg-subtle" aria-label={label} /></span>;
  if (phase === "review") return <span title={label}><StatusDot tone="brand" /></span>;
  return null;
}

function ThreadRow({
  thread,
  run,
  active,
  disabled,
  deletable,
  onSelect,
  onDelete
}: {
  thread: ChatThread;
  run: OperatorRun | null;
  active: boolean;
  disabled: boolean;
  /** False while chat navigation is locked: nothing can be deleted then, so nothing offers it. */
  deletable: boolean;
  onSelect: () => void;
  onDelete: () => Promise<void>;
}) {
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const rowRef = useRef<HTMLLIElement>(null);
  const phase = runPhase(run);
  const title = threadTitle(thread, run);
  const cost = formatCostUsd(threadCostUsd(thread, run));

  useEffect(() => {
    if (!confirming) return;
    const cancel = (event: PointerEvent | KeyboardEvent) => {
      if (event instanceof KeyboardEvent ? event.key === "Escape" : !rowRef.current?.contains(event.target as Node)) setConfirming(false);
    };
    document.addEventListener("pointerdown", cancel);
    document.addEventListener("keydown", cancel);
    return () => {
      document.removeEventListener("pointerdown", cancel);
      document.removeEventListener("keydown", cancel);
    };
  }, [confirming]);

  if (confirming) {
    return (
      <li ref={rowRef} className="flex h-9 animate-enter-fade items-center gap-1.5 rounded-lg bg-danger/[0.08] pl-2.5 pr-1">
        <span className="min-w-0 flex-1 truncate text-13 text-fg-muted">Delete this chat?</span>
        <Button
          variant="danger"
          size="xs"
          loading={deleting}
          autoFocus
          onClick={async () => {
            setDeleting(true);
            try {
              await onDelete();
            } finally {
              setDeleting(false);
              setConfirming(false);
            }
          }}
        >
          Delete
        </Button>
        <Button variant="ghost" size="xs" onClick={() => setConfirming(false)}>
          Keep
        </Button>
      </li>
    );
  }

  return (
    <li ref={rowRef} className="group/row relative">
      <button
        type="button"
        onClick={onSelect}
        disabled={disabled}
        aria-current={active ? "page" : undefined}
        title={cost ? `${title} · ${cost} model spend` : title}
        className={cn(
          "flex h-9 w-full items-center gap-2 rounded-lg pl-2.5 pr-2 text-left text-13 transition-colors focus-ring-inset disabled:cursor-not-allowed disabled:opacity-45",
          active ? "bg-fg/[0.08] font-medium text-fg" : "text-fg-muted hover:bg-fg/[0.05] hover:text-fg"
        )}
      >
        <span className="min-w-0 flex-1 truncate">{title}</span>
        <span className="flex shrink-0 items-center gap-1.5 transition-opacity group-hover/row:opacity-0 group-focus-within/row:opacity-0">
          {cost ? <span className="text-2xs font-normal tabular-nums text-fg-subtle">{cost}</span> : null}
          <PhaseIndicator phase={phase} />
        </span>
      </button>
      {!disabled && deletable ? (
        <button
          type="button"
          aria-label={`Delete “${title}”`}
          onClick={() => setConfirming(true)}
          className="absolute right-1 top-1/2 flex size-7 -translate-y-1/2 items-center justify-center rounded-md text-fg-subtle opacity-0 transition hover:bg-danger/10 hover:text-danger-ink focus:opacity-100 focus-ring group-hover/row:opacity-100"
        >
          <Trash2 className="size-3.5" />
        </button>
      ) : null}
    </li>
  );
}

function RailButton({ label, shortcut, onClick, disabled, children, active }: { label: string; shortcut?: string[]; onClick: () => void; disabled?: boolean; children: ReactNode; active?: boolean }) {
  return (
    <Tooltip label={label} shortcut={shortcut} side="right">
      <button
        type="button"
        onClick={onClick}
        disabled={disabled}
        aria-label={label}
        className={cn(
          "flex size-9 items-center justify-center rounded-lg text-fg-muted transition hover:bg-fg/[0.06] hover:text-fg focus-ring disabled:cursor-not-allowed disabled:opacity-40 [&_svg]:size-[18px]",
          active && "bg-fg/[0.08] text-fg"
        )}
      >
        {children}
      </button>
    </Tooltip>
  );
}

export function Sidebar({
  collapsed,
  onToggle,
  threads,
  threadsLoaded,
  runsById,
  activeThreadId,
  onNewChat,
  onSearch,
  onSelectThread,
  onDeleteThread,
  onOpenPanel,
  navigationLocked,
  secureEntryActive = false,
  footer
}: {
  collapsed: boolean;
  onToggle: () => void;
  threads: ChatThread[];
  threadsLoaded: boolean;
  runsById: Map<number, OperatorRun>;
  activeThreadId: number | null;
  onNewChat: () => void;
  onSearch: () => void;
  onSelectThread: (thread: ChatThread) => void;
  onDeleteThread: (thread: ChatThread) => Promise<void>;
  onOpenPanel: (key: PanelKey) => void;
  navigationLocked: boolean;
  /** Navigation is locked because a secure entry is in progress (not just while connecting). */
  secureEntryActive?: boolean;
  /** Extra footer content (the desktop status), shown under the panel links. */
  footer?: ReactNode;
}) {
  const isMac = useIsMac();
  const groups = useMemo(() => groupByRecency(threads), [threads]);
  const attentionCount = useMemo(
    () => threads.filter((thread) => thread.run_id != null && runPhase(runsById.get(thread.run_id)) === "needs_you").length,
    [threads, runsById]
  );

  if (collapsed) {
    return (
      <nav aria-label="Lumen" className="flex h-full w-[60px] shrink-0 flex-col items-center gap-1 border-r border-line bg-sidebar py-3">
        <RailButton label="Expand sidebar" shortcut={["Ctrl", "B"]} onClick={onToggle}>
          <PanelLeftOpen />
        </RailButton>
        <div className="my-1.5 h-px w-6 bg-line" />
        <RailButton label="New task" shortcut={["Ctrl", "Shift", "O"]} onClick={onNewChat} disabled={navigationLocked}>
          <SquarePen />
        </RailButton>
        <RailButton label="Search chats" shortcut={["Ctrl", "K"]} onClick={onSearch} disabled={navigationLocked}>
          <span className="relative">
            <Search />
            {attentionCount ? <StatusDot tone="attention" className="absolute -right-1 -top-1 size-1.5 [&>span]:size-1.5" /> : null}
          </span>
        </RailButton>
        <div className="mt-auto flex flex-col items-center gap-1">
          {PANELS.map((panel) => (
            <RailButton key={panel.key} label={panel.label} shortcut={panel.shortcut} onClick={() => onOpenPanel(panel.key)}>
              <panel.icon />
            </RailButton>
          ))}
          {footer ? <div className="mt-1">{footer}</div> : null}
        </div>
      </nav>
    );
  }

  return (
    <nav aria-label="Lumen" className="flex h-full w-[264px] shrink-0 flex-col border-r border-line bg-sidebar">
      <div className="flex h-14 shrink-0 items-center justify-between gap-2 pl-4 pr-2.5">
        <div className="flex items-center gap-2.5">
          <LumenMark className="size-[22px]" />
          <LumenWordmark />
        </div>
        <Tooltip label="Collapse sidebar" shortcut={["Ctrl", "B"]} side="bottom">
          <Button variant="ghost" size="icon-sm" aria-label="Collapse sidebar" onClick={onToggle}>
            <PanelLeftClose />
          </Button>
        </Tooltip>
      </div>

      <div className="space-y-0.5 px-2.5 pb-3">
        <button
          type="button"
          onClick={onNewChat}
          disabled={navigationLocked}
          className="group/new flex h-9 w-full items-center gap-2.5 rounded-lg border border-line-strong bg-surface-1 px-2.5 text-13 font-medium text-fg shadow-xs transition hover:border-fg-subtle/40 hover:bg-surface-2 focus-ring disabled:cursor-not-allowed disabled:opacity-45"
        >
          <SquarePen className="size-4 text-fg-muted" />
          New task
          <span className="ml-auto hidden items-center gap-0.5 opacity-0 transition group-hover/new:opacity-100 xl:flex">
            <Kbd>{isMac ? "⌘" : "Ctrl"}</Kbd>
            <Kbd>⇧</Kbd>
            <Kbd>O</Kbd>
          </span>
        </button>
        <button
          type="button"
          onClick={onSearch}
          disabled={navigationLocked}
          className="flex h-9 w-full items-center gap-2.5 rounded-lg px-2.5 text-13 text-fg-muted transition hover:bg-fg/[0.05] hover:text-fg focus-ring disabled:cursor-not-allowed disabled:opacity-45"
        >
          <Search className="size-4" />
          Search
          <span className="ml-auto flex items-center gap-0.5">
            <Kbd>{isMac ? "⌘" : "Ctrl"}</Kbd>
            <Kbd>K</Kbd>
          </span>
        </button>
      </div>

      <div className="scroll-area fade-y min-h-0 flex-1 overflow-y-auto px-2.5 pb-4">
        {secureEntryActive ? (
          <p className="mx-1 mb-3 rounded-lg border border-brand/20 bg-brand/[0.06] px-3 py-2 text-xs text-fg-muted">Finish the secure entry to switch chats.</p>
        ) : null}
        {!threadsLoaded ? (
          <div className="space-y-1.5 px-1 pt-2" aria-hidden="true">
            {[72, 56, 64, 48].map((width) => (
              <div key={width} className="h-3 animate-pulse rounded bg-fg/[0.06]" style={{ width: `${width}%` }} />
            ))}
          </div>
        ) : groups.length ? (
          groups.map((group) => (
            <div key={group.label} className="mb-3">
              <p className="eyebrow px-2.5 pb-1.5 pt-2">{group.label}</p>
              <ul className="space-y-px">
                {group.items.map((thread) => (
                  <ThreadRow
                    key={thread.id}
                    thread={thread}
                    run={thread.run_id != null ? runsById.get(thread.run_id) ?? null : null}
                    active={thread.id === activeThreadId}
                    disabled={navigationLocked && thread.id !== activeThreadId}
                    deletable={!navigationLocked}
                    onSelect={() => onSelectThread(thread)}
                    onDelete={() => onDeleteThread(thread)}
                  />
                ))}
              </ul>
            </div>
          ))
        ) : (
          <p className="px-2.5 pt-2 text-13 text-fg-subtle">Your tasks and chats will show up here.</p>
        )}
      </div>

      <div className="shrink-0 border-t border-line px-2.5 py-2.5">
        <ul className="space-y-px">
          {PANELS.map((panel) => (
            <li key={panel.key}>
              <button
                type="button"
                onClick={() => onOpenPanel(panel.key)}
                className="flex h-8 w-full items-center gap-2.5 rounded-lg px-2.5 text-13 text-fg-muted transition hover:bg-fg/[0.05] hover:text-fg focus-ring"
              >
                <panel.icon className="size-4" />
                {panel.label}
              </button>
            </li>
          ))}
        </ul>
        {footer ? <div className="mt-2 px-0.5">{footer}</div> : null}
      </div>
    </nav>
  );
}
