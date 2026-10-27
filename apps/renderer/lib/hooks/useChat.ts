"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { dedupeChatMessages } from "@/lib/chatMessages";
import { api, type ChatMessage, type ChatStreamMeta, type ChatThread, type OperatorQuestion, type OperatorRun, type QuestionArtifactResult } from "@/lib/api";

let clientSeq = 0;
function nextClientId() {
  clientSeq += 1;
  return -clientSeq;
}

function optimistic(id: number, role: "user" | "assistant", content: string, clientStatus?: string, retryContent?: string): ChatMessage {
  return {
    id,
    thread_id: 0,
    role,
    content,
    payload_json: clientStatus ? { client_status: clientStatus, ...(retryContent ? { retry_content: retryContent } : {}) } : {},
    linked_event_id: null,
    linked_task_id: null,
    created_at: new Date().toISOString()
  };
}

function errorText(error: unknown) {
  return error instanceof Error ? error.message : "Lumen could not respond. Please try again.";
}

const STOPPED_RUN_STATES = new Set(["BLOCKED", "WAITING_FOR_USER", "READY_FOR_REVIEW", "COMPLETED", "FAILED"]);
const CHAT_SEND_TIMEOUT_MS = 120000;

function isLoadingMessage(message: ChatMessage): boolean {
  return message.role === "assistant" && message.payload_json?.client_status === "loading";
}

function runStateContent(run: OperatorRun): string {
  const state = String(run.current_state || "").toUpperCase();
  const detail = run.status_message?.trim();
  if (state === "BLOCKED") return detail ? `Blocked: ${detail}` : "Blocked: Lumen needs attention before it can continue.";
  if (state === "WAITING_FOR_USER") return detail ? `Lumen needs your input: ${detail}` : "Lumen needs your input before it can continue.";
  if (state === "READY_FOR_REVIEW") {
    if (detail) return `Ready for review: ${detail}`;
    return "Ready for review. Please inspect the browser task before any consequential final action.";
  }
  if (state === "COMPLETED") return detail ? `Completed: ${detail}` : "Completed.";
  if (state === "FAILED") return detail ? `Failed: ${detail}` : "Failed.";
  return detail || "Lumen stopped working on this run.";
}

export type SensitiveBrowserTakeover = {
  runId: number;
  targetUrl: string;
  thread: ChatThread | null;
  handoffId: string;
  kind: string;
  /** Where the value is typed: a Chrome page, or an app window on the desktop. */
  entrySurface: "browser" | "desktop";
  credentialOrigin: string;
  credentialSlot: string;
};

function activeSensitiveBrowserTakeover(
  run: OperatorRun
): Pick<SensitiveBrowserTakeover, "handoffId" | "kind" | "entrySurface" | "credentialOrigin" | "credentialSlot"> | null {
  const raw = run.confidence_state?.sensitive_browser_takeover;
  if (!raw || typeof raw !== "object" || raw.active !== true) return null;
  return {
    handoffId: typeof raw.handoff_id === "string" ? raw.handoff_id.trim() : "",
    kind: typeof raw.kind === "string" && raw.kind.trim() ? raw.kind.trim() : "direct_entry",
    entrySurface: raw.entry_surface === "desktop" ? "desktop" : "browser",
    credentialOrigin: typeof raw.credential_origin === "string" ? raw.credential_origin.trim() : "",
    credentialSlot: typeof raw.credential_slot === "string" ? raw.credential_slot.trim() : ""
  };
}

export function useChat() {
  // Fresh app loads start clean except for an active sensitive-value handoff,
  // whose owning thread is restored so its secure controls cannot be stranded.
  const [threadId, setThreadId] = useState<number | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sending, setSending] = useState(false);
  const [pendingQuestion, setPendingQuestion] = useState<OperatorQuestion | null>(null);
  const [activeRunId, setActiveRunId] = useState<number | null>(null);
  // The latest recent-runs list from the sensitive-takeover poll, shared so the chat list can show
  // each chat's run state without a second poll.
  const [runs, setRuns] = useState<OperatorRun[]>([]);
  const [sensitiveTakeover, setSensitiveTakeover] = useState<SensitiveBrowserTakeover | null>(null);
  const [sensitiveTakeoverDiscoveryComplete, setSensitiveTakeoverDiscoveryComplete] = useState(false);
  const sendingRef = useRef(false);
  const threadRef = useRef<number | null>(null);
  const activeRunIdRef = useRef<number | null>(null);
  const sensitiveTakeoverRef = useRef<SensitiveBrowserTakeover | null>(null);
  const sensitiveTakeoverDiscoveryCompleteRef = useRef(false);
  const sensitiveTakeoverRefreshInFlightRef = useRef(false);
  const sessionEpochRef = useRef(0);
  const sensitiveTakeoverLocked = !sensitiveTakeoverDiscoveryComplete || sensitiveTakeover !== null;
  sensitiveTakeoverRef.current = sensitiveTakeover;
  sensitiveTakeoverDiscoveryCompleteRef.current = sensitiveTakeoverDiscoveryComplete;

  const isCurrentEpoch = useCallback((epoch: number) => sessionEpochRef.current === epoch, []);

  const bindActiveRun = useCallback((runId: number | null | undefined) => {
    const normalized = Number(runId ?? 0);
    if (!Number.isFinite(normalized) || normalized <= 0) return;
    activeRunIdRef.current = normalized;
    setActiveRunId(normalized);
  }, []);

  const refreshPendingQuestion = useCallback(async (runId: number): Promise<OperatorQuestion | null> => {
    const epoch = sessionEpochRef.current;
    try {
      const questions = await api.runQuestions(runId);
      const nextQuestion = questions.find((question) => question.status === "pending") ?? null;
      // Only write if we're still in the SAME session. An in-flight reconcile started before "New
      // chat" must not write a prior run's pending question into the fresh, empty chat.
      if (isCurrentEpoch(epoch)) setPendingQuestion(nextQuestion);
      return nextQuestion;
    } catch {
      // Run binding should still render if question refresh is temporarily unavailable.
      return null;
    }
  }, [isCurrentEpoch]);

  const clearSendingForEpoch = useCallback(
    (epoch: number) => {
      if (!isCurrentEpoch(epoch)) return;
      sendingRef.current = false;
      setSending(false);
    },
    [isCurrentEpoch]
  );

  const adoptPersistedThread = useCallback(
    async (thread: ChatThread, options: { replaceMessages?: boolean } = {}) => {
      threadRef.current = thread.id;
      setThreadId(thread.id);
      if (thread.run_id) {
        bindActiveRun(thread.run_id);
        await refreshPendingQuestion(thread.run_id);
      } else {
        activeRunIdRef.current = null;
        setActiveRunId(null);
        setPendingQuestion(null);
      }
      if (options.replaceMessages) {
        setMessages(dedupeChatMessages(await api.chatMessages(thread.id)));
      }
    },
    [bindActiveRun, refreshPendingQuestion]
  );

  const reconcilePersistedThreadState = useCallback(
    async (tid: number, options: { replaceMessages?: boolean } = {}) => {
      try {
        const [threads, existing] = await Promise.all([api.chatThreads(), api.chatMessages(tid)]);
        const persisted = threads.find((thread) => thread.id === tid);
        if (persisted?.run_id) {
          await adoptPersistedThread(persisted);
        }
        // While a send is streaming, the optimistic list (user message +
        // "Thinking…" placeholder) is authoritative. The persisted server
        // messages never contain the client-only loading placeholder, so an
        // implicit replace here would wipe the thinking indicator mid-stream.
        // Only replace when explicitly asked (final reconcile / error path) or
        // when no send is in flight.
        if (options.replaceMessages || (!sendingRef.current && !existing.some(isLoadingMessage))) {
          setMessages(dedupeChatMessages(existing));
        }
      } catch {
        // Keep the optimistic stream state if persisted reconciliation is temporarily unavailable.
      }
    },
    [adoptPersistedThread]
  );

  const startPersistedRunBindingPoll = useCallback(
    (tid: number, epoch: number) => {
      let stopped = false;
      let interval: number | null = null;
      const poll = async () => {
        if (stopped || !isCurrentEpoch(epoch)) return;
        await reconcilePersistedThreadState(tid);
      };
      void poll();
      interval = window.setInterval(() => {
        if (isCurrentEpoch(epoch)) void poll();
      }, 1000);
      return () => {
        stopped = true;
        if (interval !== null) window.clearInterval(interval);
      };
    },
    [isCurrentEpoch, reconcilePersistedThreadState]
  );

  const send = useCallback(async (raw: string, extra?: Record<string, unknown>) => {
    const content = raw.trim();
    if (
      !content
      || sendingRef.current
      || !sensitiveTakeoverDiscoveryCompleteRef.current
      || sensitiveTakeoverRef.current
    ) return;

    // Everything goes through one chat loop. The backend's model-owned intent contract decides
    // whether a message starts a generic browser task; the frontend has no task-specific fast path.
    const requestEpoch = sessionEpochRef.current;
    sendingRef.current = true;
    setSending(true);
    const userId = nextClientId();
    const loadingId = nextClientId();
    let stopPersistedRunBindingPoll: (() => void) | null = null;
    let timedOut = false;
    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
      clearSendingForEpoch(requestEpoch);
    }, CHAT_SEND_TIMEOUT_MS);
    setMessages((prev) => [...prev, optimistic(userId, "user", content, "sending"), optimistic(loadingId, "assistant", "", "loading", content)]);
    try {
      let tid = threadRef.current;
      if (!tid) {
        const created = await api.createChatThread();
        if (!isCurrentEpoch(requestEpoch)) return;
        tid = created.id;
        threadRef.current = created.id;
        setThreadId(created.id);
      }
      stopPersistedRunBindingPoll = startPersistedRunBindingPoll(tid, requestEpoch);
      await api.streamChatMessage(
        tid,
        content,
        // extra carries a structured confirm-ask payload (e.g. {keep_proposed, task_ids}) so the
        // backend applies the agent's proposed answer verbatim when the user clicks "Approve".
        { source: "user_mode_chat", view_mode: "user", ...(extra || {}) },
        {
          onMeta: (meta: ChatStreamMeta) => {
            if (!isCurrentEpoch(requestEpoch)) return;
            setMessages((prev) => prev.map((m) => (m.id === userId ? meta.user_message : m)));
            setPendingQuestion(meta.pending_question ?? null);
            const started = Number(
              (meta.task_request as { run_id?: number } | null)?.run_id
              ?? (meta.application_request as { run_id?: number } | null)?.run_id
              ?? 0
            );
            if (started) bindActiveRun(started);
          },
          onToken: (delta) => {
            if (!isCurrentEpoch(requestEpoch)) return;
            setMessages((prev) => prev.map((m) => (m.id === loadingId ? { ...m, content: m.content + delta } : m)));
          },
          onDone: (done) => {
            if (!isCurrentEpoch(requestEpoch)) return;
            const doneRunId = Number((done.chat_response as { active_run_id?: number } | null)?.active_run_id ?? 0);
            if (doneRunId) bindActiveRun(doneRunId);
            setMessages((prev) => prev.map((m) => (m.id === loadingId ? done.assistant_message : m)));
          },
          signal: controller.signal
        }
      );
      stopPersistedRunBindingPoll();
      stopPersistedRunBindingPoll = null;
      if (isCurrentEpoch(requestEpoch)) {
        await reconcilePersistedThreadState(tid, { replaceMessages: true });
      }
    } catch (err) {
      if (isCurrentEpoch(requestEpoch)) {
        const text = timedOut ? "Lumen chat response timed out. You can send again." : errorText(err);
        setMessages((prev) => prev.map((m) => (m.id === loadingId ? optimistic(loadingId, "assistant", text, "error", content) : m)));
        const tid = threadRef.current;
        if (tid) void reconcilePersistedThreadState(tid, { replaceMessages: true });
      }
    } finally {
      window.clearTimeout(timeoutId);
      stopPersistedRunBindingPoll?.();
      clearSendingForEpoch(requestEpoch);
    }
  }, [bindActiveRun, clearSendingForEpoch, isCurrentEpoch, reconcilePersistedThreadState, startPersistedRunBindingPoll]);

  const retry = useCallback(
    (message: ChatMessage) => {
      const retryContent = message.payload_json?.retry_content;
      if (typeof retryContent === "string" && retryContent.trim()) {
        setMessages((prev) => prev.filter((m) => m.id !== message.id));
        void send(retryContent);
      }
    },
    [send]
  );

  const adoptQuestionArtifactResult = useCallback(
    async (result: QuestionArtifactResult) => {
      const runId = Number(result.run?.id ?? result.question?.run_id ?? 0);
      if (runId) {
        bindActiveRun(runId);
        await refreshPendingQuestion(runId);
      } else if (result.question) {
        setPendingQuestion(result.question.status === "pending" ? result.question : null);
      }
      const tid = threadRef.current;
      if (tid) {
        await reconcilePersistedThreadState(tid, { replaceMessages: true });
      }
    },
    [bindActiveRun, reconcilePersistedThreadState, refreshPendingQuestion]
  );

  const answerFinalAction = useCallback(
    async (
      questionId: number,
      approvalToken: string,
      decision: "approve" | "decline",
      answer: string
    ) => {
      if (
        sendingRef.current
        || !sensitiveTakeoverDiscoveryCompleteRef.current
        || sensitiveTakeoverRef.current
      ) return;
      const requestEpoch = sessionEpochRef.current;
      sendingRef.current = true;
      setSending(true);
      try {
        const result = await api.answerFinalAction(questionId, { answer, approvalToken, decision });
        if (!isCurrentEpoch(requestEpoch)) return;
        bindActiveRun(result.run.id);
        await refreshPendingQuestion(result.run.id);
        const tid = threadRef.current;
        if (tid) await reconcilePersistedThreadState(tid, { replaceMessages: true });
      } catch {
        // The approval question stays pending, so its card remains available to retry.
      } finally {
        clearSendingForEpoch(requestEpoch);
      }
    },
    [bindActiveRun, clearSendingForEpoch, isCurrentEpoch, reconcilePersistedThreadState, refreshPendingQuestion]
  );

  const loadThread = useCallback(async (thread: ChatThread) => {
    const takeover = sensitiveTakeoverRef.current;
    if (
      !sensitiveTakeoverDiscoveryCompleteRef.current
      || (takeover && takeover.thread?.id !== thread.id)
    ) return;
    sessionEpochRef.current += 1;
    const requestEpoch = sessionEpochRef.current;
    sendingRef.current = false;
    setSending(false);
    try {
      const existing = await api.chatMessages(thread.id);
      if (!isCurrentEpoch(requestEpoch)) return;
      await adoptPersistedThread(thread);
      if (!isCurrentEpoch(requestEpoch)) return;
      setMessages(dedupeChatMessages(existing));
    } catch {
      // The previously shown messages stay in place when the thread cannot be loaded.
    }
  }, [adoptPersistedThread, isCurrentEpoch]);

  const refreshSensitiveTakeover = useCallback(async () => {
    if (sensitiveTakeoverRefreshInFlightRef.current) return;
    sensitiveTakeoverRefreshInFlightRef.current = true;
    try {
      const runs = await api.runs();
      setRuns(runs);
      const active = runs
        .map((run) => {
          const marker = activeSensitiveBrowserTakeover(run);
          return marker ? { run, ...marker } : null;
        })
        .filter(
          (
            item
          ): item is {
            run: OperatorRun;
            handoffId: string;
            kind: string;
            entrySurface: "browser" | "desktop";
            credentialOrigin: string;
            credentialSlot: string;
          } => item !== null
        )
        .sort((left, right) => right.run.id - left.run.id)[0];

      if (active) {
        const provisional: SensitiveBrowserTakeover = {
          runId: active.run.id,
          targetUrl: active.run.target_url,
          thread: null,
          handoffId: active.handoffId,
          kind: active.kind,
          entrySurface: active.entrySurface,
          credentialOrigin: active.credentialOrigin,
          credentialSlot: active.credentialSlot
        };
        sensitiveTakeoverRef.current = provisional;
        sensitiveTakeoverDiscoveryCompleteRef.current = true;
        setSensitiveTakeover(provisional);
        setSensitiveTakeoverDiscoveryComplete(true);

        let thread: ChatThread | null = null;
        try {
          thread = (await api.chatThreads(active.run.id)).find(
            (item) => item.run_id === active.run.id
          ) ?? null;
        } catch {
          // Exact run controls remain available if the owning thread is temporarily unavailable.
        }
        const next = { ...provisional, thread };
        sensitiveTakeoverRef.current = next;
        setSensitiveTakeover(next);
      } else {
        sensitiveTakeoverRef.current = null;
        sensitiveTakeoverDiscoveryCompleteRef.current = true;
        setSensitiveTakeover(null);
        setSensitiveTakeoverDiscoveryComplete(true);
      }
    } catch {
      // Fail closed while the run list cannot prove that no direct-entry handoff is active.
      sensitiveTakeoverDiscoveryCompleteRef.current = false;
      setSensitiveTakeoverDiscoveryComplete(false);
    } finally {
      sensitiveTakeoverRefreshInFlightRef.current = false;
    }
  }, []);

  useEffect(() => {
    void refreshSensitiveTakeover();
    const interval = window.setInterval(() => {
      void refreshSensitiveTakeover();
    }, 2500);
    return () => window.clearInterval(interval);
  }, [refreshSensitiveTakeover]);

  useEffect(() => {
    const ownerThread = sensitiveTakeover?.thread;
    if (!sensitiveTakeover || !ownerThread) return;
    if (
      threadRef.current === ownerThread.id
      && activeRunIdRef.current === sensitiveTakeover.runId
    ) return;
    void loadThread(ownerThread);
  }, [loadThread, sensitiveTakeover?.runId, sensitiveTakeover?.thread?.id]);

  const reconcileRunState = useCallback(async (run: OperatorRun | null) => {
    if (!run || !STOPPED_RUN_STATES.has(String(run.current_state || "").toUpperCase())) return;
    const state = String(run.current_state || "").toUpperCase();
    let refreshedQuestion: OperatorQuestion | null = null;
    if (state === "BLOCKED" || state === "WAITING_FOR_USER") {
      try {
        refreshedQuestion = await refreshPendingQuestion(run.id);
      } catch {
        // The state message still makes the stopped run visible if question refresh is temporarily unavailable.
      }
    }
    if (state === "WAITING_FOR_USER" || refreshedQuestion) {
      sendingRef.current = false;
      setSending(false);
    }
    const hasLoading = messages.some(isLoadingMessage);
    if (!hasLoading) return;

    const tid = threadRef.current;
    if (tid) {
      try {
        const existing = await api.chatMessages(tid);
        if (!existing.some(isLoadingMessage)) {
          setMessages(dedupeChatMessages(existing));
          sendingRef.current = false;
          setSending(false);
          return;
        }
      } catch {
        // Fall through to a deterministic run-state message if persisted messages are temporarily unavailable.
      }
    }

    const replacement = optimistic(nextClientId(), "assistant", runStateContent(run), "state");
    setMessages((prev) => prev.map((message) => (isLoadingMessage(message) ? { ...replacement, id: message.id, thread_id: message.thread_id } : message)));
    sendingRef.current = false;
    setSending(false);
  }, [messages, refreshPendingQuestion]);

  const reset = useCallback(() => {
    if (!sensitiveTakeoverDiscoveryCompleteRef.current || sensitiveTakeoverRef.current) return;
    sessionEpochRef.current += 1;
    sendingRef.current = false;
    threadRef.current = null;
    activeRunIdRef.current = null;
    setThreadId(null);
    setActiveRunId(null);
    setMessages([]);
    setPendingQuestion(null);
    setSending(false);
  }, []);

  // Resolves false when deletion is blocked (secure entry, or the lock state is not yet known).
  const deleteThread = useCallback(
    async (threadId: number): Promise<boolean> => {
      if (!sensitiveTakeoverDiscoveryCompleteRef.current || sensitiveTakeoverRef.current) return false;
      await api.deleteChatThread(threadId);
      if (threadRef.current === threadId) {
        reset();
      }
      return true;
    },
    [reset]
  );

  // While a run is active, the browser task executes in the background and posts
  // chat messages (start ack, then outcome) as it progresses. Poll the thread's
  // messages so that narration appears live, without clobbering an in-flight send.
  useEffect(() => {
    if (activeRunId == null) return;
    let active = true;
    const interval = window.setInterval(async () => {
      if (!active || sendingRef.current) return;
      const tid = threadRef.current;
      if (!tid) return;
      try {
        const existing = await api.chatMessages(tid);
        if (active && !sendingRef.current && !existing.some(isLoadingMessage)) {
          setMessages(dedupeChatMessages(existing));
        }
      } catch {
        // transient; next tick retries
      }
    }, 2500);
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, [activeRunId]);

  return {
    threadId,
    messages,
    sending,
    pendingQuestion,
    activeRunId,
    runs,
    sensitiveTakeover,
    sensitiveTakeoverLocked,
    sensitiveTakeoverDiscoveryComplete,
    send,
    answerFinalAction,
    retry,
    reset,
    loadThread,
    deleteThread,
    reconcileRunState,
    refreshSensitiveTakeover,
    adoptQuestionArtifactResult
  };
}
