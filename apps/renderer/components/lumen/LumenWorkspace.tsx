"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle } from "lucide-react";
import { useChat } from "@/lib/hooks/useChat";
import { useActiveRun } from "@/lib/hooks/useActiveRun";
import { useDesktopStatus } from "@/lib/hooks/useDesktopStatus";
import { useThreads } from "@/lib/hooks/useThreads";
import { useMediaQuery } from "@/lib/hooks/useMediaQuery";
import { api, type ChatThread, type OperatorRun } from "@/lib/api";
import { runIsLive } from "@/lib/format";
import { runPhase } from "@/lib/runPhase";
import { threadTitle } from "@/lib/threads";
import { formatCostUsd, threadCostUsd } from "@/lib/cost";
import { Sidebar, type PanelKey } from "@/components/lumen/shell/Sidebar";
import { TopBar } from "@/components/lumen/shell/TopBar";
import { SystemStatus } from "@/components/lumen/shell/SystemStatus";
import { HomeScreen, type WaitingItem } from "@/components/lumen/home/HomeScreen";
import { CommandMenu } from "@/components/lumen/CommandMenu";
import { ChatStream } from "@/components/lumen/chat/ChatStream";
import { ChatComposer, type ComposerHandle } from "@/components/lumen/chat/ChatComposer";
import { RunDock } from "@/components/lumen/chat/RunDock";
import { RunTimelineBlock } from "@/components/lumen/chat/RunTimelineBlock";
import { CredentialVaultHandoffCard } from "@/components/lumen/chat/CredentialVaultHandoffCard";
import { SensitiveEntryCard } from "@/components/lumen/chat/SensitiveEntryCard";
import { ProfileDrawer } from "@/components/lumen/drawers/ProfileDrawer";
import { ArtifactsDrawer } from "@/components/lumen/drawers/ArtifactsDrawer";
import { CredentialsDrawer } from "@/components/lumen/drawers/CredentialsDrawer";
import { SettingsDialog, type SettingsSection } from "@/components/lumen/drawers/SettingsDialog";
import { Button } from "@/components/ui/Button";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Toaster, toast } from "@/components/ui/toast";

const SIDEBAR_KEY = "lumen.sidebar";

function useSidebarCollapsed() {
  const [collapsed, setCollapsed] = useState(false);
  useEffect(() => {
    try {
      setCollapsed(localStorage.getItem(SIDEBAR_KEY) === "collapsed");
    } catch {
      // Storage unavailable: start expanded.
    }
  }, []);
  const toggle = useCallback(() => {
    setCollapsed((current) => {
      const next = !current;
      try {
        localStorage.setItem(SIDEBAR_KEY, next ? "collapsed" : "expanded");
      } catch {
        // The choice still applies for this session.
      }
      return next;
    });
  }, []);
  return [collapsed, toggle] as const;
}

function firstNameOf(name: string): string {
  return name.trim().split(/\s+/)[0] || "";
}

export function LumenWorkspace() {
  const chat = useChat();
  const effectiveRunId = chat.sensitiveTakeover?.runId ?? chat.activeRunId;
  const active = useActiveRun(effectiveRunId);
  const desktop = useDesktopStatus();
  const hasMessages = chat.messages.length > 0;
  const { threads, loaded: threadsLoaded, refresh: refreshThreads, forget } = useThreads(`${chat.threadId}:${hasMessages}`);
  const [panel, setPanel] = useState<PanelKey | null>(null);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("approvals");
  const [commandOpen, setCommandOpen] = useState(false);
  const [collapsed, toggleSidebar] = useSidebarCollapsed();
  // Narrow windows keep the icon rail; expanding it opens the full sidebar over the content.
  const narrow = useMediaQuery("(max-width: 899px)");
  const [sidebarOverlay, setSidebarOverlay] = useState(false);
  const [firstName, setFirstName] = useState("");
  const [modelsMissing, setModelsMissing] = useState(false);
  const composerRef = useRef<ComposerHandle>(null);
  // Chat stays locked until the backend confirms no secure entry is waiting; if that takes more
  // than a few seconds, say so instead of an endless "Connecting".
  const [connectionSlow, setConnectionSlow] = useState(false);

  const locked = chat.sensitiveTakeoverLocked;
  const sensitiveTakeoverActive = chat.sensitiveTakeover !== null;
  const sensitiveTakeoverHandoffId = chat.sensitiveTakeover?.handoffId ?? "";
  const displayedPendingQuestion = chat.activeRunId === effectiveRunId ? chat.pendingQuestion : null;

  // The recent-runs poll gives every chat its state; the bound run's own 3 s poll is fresher.
  const runsById = useMemo(() => {
    const map = new Map<number, OperatorRun>(chat.runs.map((run) => [run.id, run]));
    if (active.run) map.set(active.run.id, active.run);
    return map;
  }, [chat.runs, active.run]);

  useEffect(() => {
    // Only reconcile when a run is actually bound. Right after "New task", activeRunId is null but
    // active.run can still be the previous (stale) run for a tick; reconciling it would re-fetch and
    // re-show a prior run's pending-question card on a fresh, empty chat.
    if (chat.activeRunId == null || active.run?.id !== chat.activeRunId) return;
    void chat.reconcileRunState(active.run);
  }, [active.run, chat.activeRunId, chat.reconcileRunState]);

  useEffect(() => {
    if (!locked) return;
    setPanel(null);
    setCommandOpen(false);
  }, [locked]);

  useEffect(() => {
    if (chat.sensitiveTakeoverDiscoveryComplete) {
      setConnectionSlow(false);
      return;
    }
    const timeout = window.setTimeout(() => setConnectionSlow(true), 6000);
    return () => window.clearTimeout(timeout);
  }, [chat.sensitiveTakeoverDiscoveryComplete]);

  // Greeting name and first-run model check, refreshed whenever a panel closes.
  useEffect(() => {
    if (panel) return;
    let current = true;
    api
      .profile()
      .then((profile) => current && setFirstName(firstNameOf(profile.user.name)))
      .catch(() => {});
    api
      .modelProviders()
      .then((response) => current && setModelsMissing(!response.items.length && !response.active?.has_api_key))
      .catch(() => {});
    return () => {
      current = false;
    };
  }, [panel]);

  const openPanel = useCallback((key: PanelKey) => {
    setPanel(key);
  }, []);

  const openModels = useCallback(() => {
    setSettingsSection("models");
    setPanel("settings");
  }, []);

  const newChat = useCallback(() => {
    if (locked) return;
    chat.reset();
    requestAnimationFrame(() => composerRef.current?.focus());
  }, [chat, locked]);

  const selectThread = useCallback(
    (thread: ChatThread) => {
      setSidebarOverlay(false);
      void chat.loadThread(thread);
    },
    [chat]
  );

  const toggleNav = useCallback(() => {
    if (narrow) setSidebarOverlay((open) => !open);
    else toggleSidebar();
  }, [narrow, toggleSidebar]);

  useEffect(() => {
    if (!narrow) setSidebarOverlay(false);
  }, [narrow]);

  const deleteThread = useCallback(
    async (thread: ChatThread) => {
      try {
        if (!(await chat.deleteThread(thread.id))) return;
        forget(thread.id);
        void refreshThreads();
      } catch {
        toast("Couldn't delete that chat", { tone: "error" });
      }
    },
    [chat, forget, refreshThreads]
  );

  const refreshRun = active.refresh;
  const send = useCallback(
    (text: string, extra?: Record<string, unknown>) => {
      void chat.send(text, extra).then(() => {
        void refreshThreads();
        void refreshRun();
      });
    },
    [chat, refreshThreads, refreshRun]
  );

  // Global shortcuts. Ctrl+K and Ctrl+, work anywhere; "/" only when not already typing.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const mod = event.ctrlKey || event.metaKey;
      const key = event.key.toLowerCase();
      const target = event.target as HTMLElement | null;
      const typing = Boolean(target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT" || target.isContentEditable));
      if (mod && key === "k") {
        event.preventDefault();
        setCommandOpen((value) => !value);
      } else if (mod && event.shiftKey && key === "o") {
        event.preventDefault();
        newChat();
      } else if (mod && !event.shiftKey && key === "b") {
        event.preventDefault();
        toggleNav();
      } else if (mod && key === ",") {
        event.preventDefault();
        setPanel("settings");
      } else if (!mod && !event.altKey && event.key === "/" && !typing && !panel && !commandOpen) {
        event.preventDefault();
        composerRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [commandOpen, newChat, panel, toggleNav]);

  const currentThread = threads.find((thread) => thread.id === chat.threadId) ?? null;
  const currentRun = effectiveRunId != null ? (active.run?.id === effectiveRunId ? active.run : runsById.get(effectiveRunId) ?? null) : null;
  const isHome = !hasMessages && chat.threadId == null && effectiveRunId == null && !sensitiveTakeoverActive;
  // The bound run's 3 s poll keeps the header's spend current while it works; the thread list's 10 s
  // refresh covers a chat whose run is not bound.
  const headerCost = isHome ? null : formatCostUsd(threadCostUsd(currentThread, currentRun));
  const showDock = effectiveRunId != null && runIsLive(active.run);
  const phase = runPhase(active.run);
  const lastAssistant = [...chat.messages].reverse().find((message) => message.role === "assistant");
  const statusInChat = Boolean(active.run?.status_message?.trim() && lastAssistant?.content.trim() === active.run.status_message.trim());

  const waiting: WaitingItem[] = useMemo(
    () =>
      threads
        .flatMap((thread) => {
          const run = thread.run_id != null ? runsById.get(thread.run_id) : undefined;
          const threadPhase = runPhase(run);
          return run && (threadPhase === "needs_you" || threadPhase === "paused") ? [{ thread, run }] : [];
        })
        .sort((a, b) => (a.run.updated_at < b.run.updated_at ? 1 : -1)),
    [threads, runsById]
  );

  const lockedMessage = !chat.sensitiveTakeoverDiscoveryComplete
    ? connectionSlow
      ? "Can't reach Lumen's runtime yet. Retrying…"
      : "Connecting to Lumen…"
    : sensitiveTakeoverActive
      ? chat.sensitiveTakeover?.kind === "credential_vault"
        ? "Chat is paused while you save the sign-in above."
        : chat.sensitiveTakeover?.entrySurface === "desktop"
          ? "Chat is paused while you enter the value in the app window."
          : "Chat is paused while you enter the value in Chrome."
      : undefined;

  const placeholder = displayedPendingQuestion
    ? "Answer here, or tell Lumen what to do instead…"
    : phase === "working"
      ? "Add a correction or instruction…"
      : "Message Lumen…";

  const statusPill = <SystemStatus desktop={desktop} onOpenModels={openModels} />;
  const hostNotice =
    desktop.problem && desktop.problem.kind !== "model-missing" ? (
      <div className="flex items-center gap-3 rounded-xl border border-attention/30 bg-attention/[0.07] px-4 py-3">
        <AlertTriangle className="size-4 shrink-0 text-attention-ink" />
        <div className="min-w-0 flex-1">
          <p className="text-13 font-medium text-fg">{desktop.problem.text}</p>
          <p className="text-xs text-fg-muted">{desktop.problem.detail}</p>
        </div>
        {desktop.problem.action ? (
          <Button size="sm" variant="secondary" loading={desktop.busy} onClick={desktop.problem.action.run}>
            {desktop.problem.action.label}
          </Button>
        ) : desktop.problem.kind === "model-unhealthy" ? (
          <Button size="sm" variant="secondary" onClick={openModels}>
            Check models
          </Button>
        ) : null}
      </div>
    ) : null;

  const composer = (
    <ChatComposer
      ref={composerRef}
      variant={isHome ? "hero" : "docked"}
      onSend={(text) => send(text)}
      sending={chat.sending}
      disabled={locked}
      lockedMessage={lockedMessage}
      placeholder={isHome ? "Describe a task, or ask a question…" : placeholder}
      dock={showDock ? <RunDock active={active} questionPending={Boolean(displayedPendingQuestion)} allowStop={!locked} /> : null}
    />
  );

  return (
    <TooltipProvider delayDuration={350} skipDelayDuration={150}>
      <div className="flex h-screen w-full overflow-hidden bg-bg text-fg">
        <Sidebar
          collapsed={collapsed || narrow}
          onToggle={toggleNav}
          threads={threads}
          threadsLoaded={threadsLoaded}
          runsById={runsById}
          activeThreadId={chat.threadId}
          onNewChat={newChat}
          onSearch={() => setCommandOpen(true)}
          onSelectThread={selectThread}
          onDeleteThread={deleteThread}
          onOpenPanel={openPanel}
          navigationLocked={locked}
          secureEntryActive={sensitiveTakeoverActive}
        />
        {narrow && sidebarOverlay ? (
          <div className="fixed inset-0 z-40 flex">
            <button type="button" aria-label="Close sidebar" className="absolute inset-0 animate-enter-fade bg-black/45" onClick={() => setSidebarOverlay(false)} />
            <div className="relative h-full animate-enter-up shadow-lg">
              <Sidebar
                collapsed={false}
                onToggle={() => setSidebarOverlay(false)}
                threads={threads}
                threadsLoaded={threadsLoaded}
                runsById={runsById}
                activeThreadId={chat.threadId}
                onNewChat={() => {
                  setSidebarOverlay(false);
                  newChat();
                }}
                onSearch={() => {
                  setSidebarOverlay(false);
                  setCommandOpen(true);
                }}
                onSelectThread={selectThread}
                onDeleteThread={deleteThread}
                onOpenPanel={(key) => {
                  setSidebarOverlay(false);
                  openPanel(key);
                }}
                navigationLocked={locked}
                secureEntryActive={sensitiveTakeoverActive}
              />
            </div>
          </div>
        ) : null}
        <main className="flex min-w-0 flex-1 flex-col">
          <TopBar title={isHome ? "" : hasMessages || currentRun ? threadTitle(currentThread ?? { title: "" }, currentRun) : ""} targetUrl={isHome ? null : currentRun?.target_url} cost={headerCost} right={statusPill} />
          {isHome ? (
            <HomeScreen
              firstName={firstName}
              composer={composer}
              onSuggestion={(prompt, select) => composerRef.current?.fill(prompt, select)}
              waiting={waiting}
              waitingDisabled={locked}
              onOpenThread={selectThread}
              needsModel={desktop.available && desktop.status ? !desktop.status.modelConfigured : modelsMissing}
              onOpenModels={openModels}
              notice={hostNotice}
            />
          ) : (
            <ChatStream
              messages={chat.messages}
              onRetry={chat.retry}
              resetKey={chat.threadId != null ? `thread:${chat.threadId}` : effectiveRunId != null ? `run:${effectiveRunId}` : "new"}
              footer={
                <div className="mx-auto w-full max-w-[46rem] px-6 pb-4">
                  {hostNotice && desktop.problem?.kind === "runtime" ? <div className="mb-3">{hostNotice}</div> : null}
                  {effectiveRunId != null && active.controlError ? (
                    <div role="alert" className="mb-3 flex animate-enter-up items-start gap-2.5 rounded-xl border border-danger/25 bg-danger/[0.06] px-4 py-2.5 text-13 text-danger-ink">
                      <AlertTriangle className="mt-0.5 size-4 shrink-0" />
                      <span>{active.controlError}</span>
                    </div>
                  ) : null}
                  {composer}
                  <p className="mt-2 text-center text-2xs text-fg-subtle">Lumen can make mistakes. It asks before anything consequential unless you choose full autonomy.</p>
                </div>
              }
            >
              <RunTimelineBlock
                runId={effectiveRunId}
                run={active.run}
                pendingQuestion={displayedPendingQuestion}
                onSelect={(text, extra) => send(text, extra)}
                onFinalAction={(questionId, approvalToken, decision, label) => {
                  void chat.answerFinalAction(questionId, approvalToken, decision, label).then(() => void refreshRun());
                }}
                onUploadArtifact={async (questionId, file) => chat.adoptQuestionArtifactResult(await api.uploadQuestionArtifact(questionId, file))}
                onGenerateArtifact={async (questionId, instructions, artifactKind) => {
                  const result = await api.generateQuestionArtifact(questionId, { instructions, artifact_kind: artifactKind });
                  await chat.adoptQuestionArtifactResult(result);
                  return result.artifact;
                }}
                onUpdateArtifact={api.updateApplicationArtifact}
                onApplyArtifact={async (questionId, artifactId) => chat.adoptQuestionArtifactResult(await api.applyQuestionArtifact(questionId, artifactId))}
                sending={chat.sending}
                statusInChat={statusInChat}
              />
              {sensitiveTakeoverActive && chat.sensitiveTakeover?.kind === "credential_vault" ? (
                <CredentialVaultHandoffCard
                  takeover={chat.sensitiveTakeover}
                  busy={active.busy}
                  onContinue={async () => {
                    if (!sensitiveTakeoverHandoffId) return false;
                    const next = await active.continueSensitiveBrowser(sensitiveTakeoverHandoffId);
                    await chat.refreshSensitiveTakeover();
                    return next !== null;
                  }}
                  onCancel={async () => {
                    await active.end();
                    await chat.refreshSensitiveTakeover();
                  }}
                />
              ) : sensitiveTakeoverActive && chat.sensitiveTakeover ? (
                <SensitiveEntryCard
                  targetUrl={chat.sensitiveTakeover.targetUrl}
                  runId={chat.sensitiveTakeover.runId}
                  entrySurface={chat.sensitiveTakeover.entrySurface}
                  busy={active.busy}
                  canContinue={Boolean(sensitiveTakeoverHandoffId)}
                  onContinue={() => {
                    void (async () => {
                      await active.continueSensitiveBrowser(sensitiveTakeoverHandoffId);
                      await chat.refreshSensitiveTakeover();
                    })();
                  }}
                  onCancel={() => {
                    void (async () => {
                      await active.end();
                      await chat.refreshSensitiveTakeover();
                    })();
                  }}
                />
              ) : null}
            </ChatStream>
          )}
        </main>
      </div>

      <CommandMenu
        open={commandOpen}
        onOpenChange={setCommandOpen}
        threads={threads}
        runsById={runsById}
        onNewChat={newChat}
        onSelectThread={selectThread}
        onOpenPanel={openPanel}
        onToggleSidebar={toggleNav}
        chatNavigationDisabled={locked}
      />
      <ProfileDrawer open={panel === "profile"} onOpenChange={(open) => setPanel(open ? "profile" : null)} />
      <ArtifactsDrawer open={panel === "documents"} onOpenChange={(open) => setPanel(open ? "documents" : null)} />
      <CredentialsDrawer open={panel === "signins"} onOpenChange={(open) => setPanel(open ? "signins" : null)} />
      <SettingsDialog
        open={panel === "settings"}
        onOpenChange={(open) => setPanel(open ? "settings" : null)}
        section={settingsSection}
        onSectionChange={setSettingsSection}
        desktop={desktop}
      />
      <Toaster />
    </TooltipProvider>
  );
}
