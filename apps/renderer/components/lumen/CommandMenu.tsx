"use client";

import { FileText, KeyRound, MessageSquare, Moon, PanelLeft, Settings, SquarePen, Sun, SunMoon, UserRound } from "lucide-react";
import type { ChatThread, OperatorRun } from "@/lib/api";
import { PHASE_META, runPhase, runTargetLabel } from "@/lib/runPhase";
import { threadTitle } from "@/lib/threads";
import { useTheme, type ThemePreference } from "@/lib/theme";
import type { PanelKey } from "@/components/lumen/shell/Sidebar";
import { Kbd } from "@/components/ui/badge";
import { CommandDialog, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList, CommandSeparator } from "@/components/ui/command";
import { platformKeys, useIsMac } from "@/lib/platform";

function Shortcut({ keys }: { keys: string[] }) {
  const isMac = useIsMac();
  return (
    <span className="ml-auto flex items-center gap-0.5">
      {platformKeys(keys, isMac).map((key) => (
        <Kbd key={key}>{key}</Kbd>
      ))}
    </span>
  );
}

const THEMES: Array<{ value: ThemePreference; label: string; icon: typeof Sun }> = [
  { value: "dark", label: "Dark", icon: Moon },
  { value: "light", label: "Light", icon: Sun },
  { value: "system", label: "Match system", icon: SunMoon }
];

export function CommandMenu({
  open,
  onOpenChange,
  threads,
  runsById,
  onNewChat,
  onSelectThread,
  onOpenPanel,
  onToggleSidebar,
  chatNavigationDisabled
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  threads: ChatThread[];
  runsById: Map<number, OperatorRun>;
  onNewChat: () => void;
  onSelectThread: (thread: ChatThread) => void;
  onOpenPanel: (key: PanelKey) => void;
  onToggleSidebar: () => void;
  chatNavigationDisabled: boolean;
}) {
  const { preference, setPreference } = useTheme();
  const run = (action: () => void) => {
    onOpenChange(false);
    action();
  };

  return (
    <CommandDialog open={open} onOpenChange={onOpenChange}>
      <CommandInput placeholder="Search chats or jump to…" />
      <CommandList>
        <CommandEmpty>Nothing matches that.</CommandEmpty>
        <CommandGroup heading="Actions">
          {!chatNavigationDisabled ? (
            <CommandItem value="new task chat" onSelect={() => run(onNewChat)}>
              <SquarePen /> New task
              <Shortcut keys={["Ctrl", "⇧", "O"]} />
            </CommandItem>
          ) : null}
          <CommandItem value="profile about me details" onSelect={() => run(() => onOpenPanel("profile"))}>
            <UserRound /> Profile
          </CommandItem>
          <CommandItem value="documents artifacts resume files" onSelect={() => run(() => onOpenPanel("documents"))}>
            <FileText /> Documents
          </CommandItem>
          <CommandItem value="sign-ins credentials passwords accounts vault" onSelect={() => run(() => onOpenPanel("signins"))}>
            <KeyRound /> Sign-ins
          </CommandItem>
          <CommandItem value="settings models approval preferences" onSelect={() => run(() => onOpenPanel("settings"))}>
            <Settings /> Settings
            <Shortcut keys={["Ctrl", ","]} />
          </CommandItem>
          <CommandItem value="toggle sidebar" onSelect={() => run(onToggleSidebar)}>
            <PanelLeft /> Toggle sidebar
            <Shortcut keys={["Ctrl", "B"]} />
          </CommandItem>
        </CommandGroup>
        {!chatNavigationDisabled && threads.length ? (
          <>
            <CommandSeparator />
            <CommandGroup heading="Recent">
              {threads.slice(0, 20).map((thread) => {
                const threadRun = thread.run_id != null ? runsById.get(thread.run_id) : undefined;
                const phase = runPhase(threadRun);
                const title = threadTitle(thread, threadRun);
                const target = runTargetLabel(threadRun?.target_url);
                return (
                  <CommandItem key={thread.id} value={`${title} ${target} ${thread.id}`} onSelect={() => run(() => onSelectThread(thread))}>
                    <MessageSquare />
                    <span className="min-w-0 flex-1 truncate">{title}</span>
                    {phase && PHASE_META[phase].live ? (
                      <span className={phase === "needs_you" ? "text-xs text-attention-ink" : "text-xs text-fg-subtle"}>{PHASE_META[phase].label}</span>
                    ) : target ? (
                      <span className="text-xs text-fg-subtle">{target}</span>
                    ) : null}
                  </CommandItem>
                );
              })}
            </CommandGroup>
          </>
        ) : null}
        <CommandSeparator />
        <CommandGroup heading="Appearance">
          {THEMES.map((theme) => (
            <CommandItem key={theme.value} value={`theme ${theme.label}`} onSelect={() => run(() => setPreference(theme.value))}>
              <theme.icon /> {theme.label}
              {preference === theme.value ? <span className="ml-auto text-xs text-brand-ink">Current</span> : null}
            </CommandItem>
          ))}
        </CommandGroup>
      </CommandList>
    </CommandDialog>
  );
}
