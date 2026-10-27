import { useFocusEffect } from "expo-router";
import { dedupeChatMessages } from "@/lib/chatMessages";
import { runIsLive } from "@/lib/format";
import { useCallback, useEffect, useRef, useState } from "react";
import { AppState } from "react-native";
import { parseBackendDate } from "./dates";
import { api, ApiError, StepUpCancelled, type AgentSettings, type ChatMessage, type ChatThread, type OperatorQuestion, type OperatorRun } from "./api";
import type { FinalActionContract } from "./questions";

/**
 * Run `fn` now and every `intervalMs` while this screen is focused and the app is in the
 * foreground; run it again the moment the app comes back.
 */
export function usePoll(fn: () => Promise<unknown>, intervalMs: number, enabled = true): void {
  const fnRef = useRef(fn);
  fnRef.current = fn;
  useFocusEffect(
    useCallback(() => {
      if (!enabled) return;
      let stopped = false;
      let timer: ReturnType<typeof setTimeout> | null = null;
      const tick = async () => {
        if (stopped) return;
        if (AppState.currentState === "active") {
          try {
            await fnRef.current();
          } catch {
            // Transient; the next tick retries.
          }
        }
        if (!stopped) timer = setTimeout(tick, intervalMs);
      };
      void tick();
      const subscription = AppState.addEventListener("change", (state) => {
        if (state === "active") void fnRef.current().catch(() => {});
      });
      return () => {
        stopped = true;
        if (timer) clearTimeout(timer);
        subscription.remove();
      };
    }, [enabled, intervalMs])
  );
}

/** Whether a paired device's Full-autonomy unlock is still live. */
export function autonomyUnlocked(device: { autonomy_until: string | null } | null | undefined): boolean {
  if (!device?.autonomy_until) return false;
  return parseBackendDate(device.autonomy_until).getTime() > Date.now();
}

/**
 * Before a phone starts a task in Full autonomy: make sure the phone's unlock is live, asking for the
 * fingerprint only when it has run out. Resolves false if the user dismissed the prompt.
 */
export async function ensureAutonomyUnlocked(): Promise<boolean> {
  const { device, autonomy_hours } = await api.me();
  if (autonomyUnlocked(device)) return true;
  try {
    await api.unlockAutonomy(autonomy_hours);
    return true;
  } catch (error) {
    if (error instanceof StepUpCancelled) return false;
    throw error;
  }
}

export function errorMessage(error: unknown, fallback = "That didn't work. Try again."): string {
  if (error instanceof StepUpCancelled) return "";
  if (error instanceof Error && error.message.trim()) return error.message.trim();
  return fallback;
}

// -------------------------------------------------------------------------------------------------
// Approval mode

export type ApprovalMode = AgentSettings["final_action_mode"];

export function useApprovalMode() {
  const [mode, setMode] = useState<ApprovalMode>("approval");
  const [saving, setSaving] = useState(false);
  const refresh = useCallback(async () => {
    const response = await api.agentSettings();
    setMode(response.settings.final_action_mode ?? "approval");
  }, []);
  useEffect(() => {
    void refresh().catch(() => {});
  }, [refresh]);
  const update = useCallback(async (next: ApprovalMode) => {
    setSaving(true);
    try {
      const response = await api.updateAgentSettings(
        { final_action_mode: next },
        next === "autonomous"
          ? { title: "Turn on Full autonomy", subtitle: "Lumen will finish requested final actions without asking. Payments still wait for you." }
          : { title: "Turn on Safe approvals", subtitle: "Lumen will wait for you before final actions." }
      );
      setMode(response.settings.final_action_mode ?? "approval");
    } finally {
      setSaving(false);
    }
  }, []);
  return { mode, saving, update, refresh };
}

// -------------------------------------------------------------------------------------------------
// One chat thread, mirroring the cockpit's useChat

let clientSeq = 0;
function clientId(): number {
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

const QUESTION_STATES = new Set(["WAITING_FOR_USER", "BLOCKED"]);
const CHAT_SEND_TIMEOUT_MS = 120000;

export type SensitiveEntry = {
  handoffId: string;
  kind: string;
  entrySurface: "browser" | "desktop";
  credentialOrigin: string;
  credentialSlot: string;
};

export function sensitiveEntryOf(run: OperatorRun | null): SensitiveEntry | null {
  const raw = run?.confidence_state?.sensitive_browser_takeover;
  if (!raw || typeof raw !== "object" || raw.active !== true) return null;
  return {
    handoffId: typeof raw.handoff_id === "string" ? raw.handoff_id.trim() : "",
    kind: typeof raw.kind === "string" && raw.kind.trim() ? raw.kind.trim() : "direct_entry",
    entrySurface: raw.entry_surface === "desktop" ? "desktop" : "browser",
    credentialOrigin: typeof raw.credential_origin === "string" ? raw.credential_origin.trim() : "",
    credentialSlot: typeof raw.credential_slot === "string" ? raw.credential_slot.trim() : ""
  };
}

export function useChatThread(initialThreadId: number | null) {
  const [threadId, setThreadId] = useState<number | null>(initialThreadId);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [thread, setThread] = useState<ChatThread | null>(null);
  const [run, setRun] = useState<OperatorRun | null>(null);
  const [question, setQuestion] = useState<OperatorQuestion | null>(null);
  const [sending, setSending] = useState(false);
  const [loaded, setLoaded] = useState(initialThreadId == null);
  const threadRef = useRef<number | null>(initialThreadId);
  const runIdRef = useRef<number | null>(null);
  const sendingRef = useRef(false);

  const refresh = useCallback(async (options: { messages?: boolean } = {}) => {
    const tid = threadRef.current;
    if (!tid) return;
    const wantMessages = options.messages ?? !sendingRef.current;
    const [threads, persisted] = await Promise.all([
      api.threads() as Promise<ChatThread[]>,
      wantMessages ? api.messages(tid) : Promise.resolve(null)
    ]);
    if (persisted && (options.messages || !sendingRef.current)) setMessages(dedupeChatMessages(persisted));
    const current = threads.find((item) => item.id === tid) ?? null;
    if (current) setThread(current);
    const runId = current?.run_id ?? runIdRef.current;
    if (runId) {
      runIdRef.current = runId;
      const current = await api.run(runId);
      setRun(current);
      if (QUESTION_STATES.has(String(current.current_state || "").toUpperCase())) {
        const questions = await api.runQuestions(runId);
        setQuestion(questions.find((item) => item.status === "pending") ?? null);
      } else {
        setQuestion(null);
      }
    }
    setLoaded(true);
  }, []);

  const live = runIsLive(run);
  usePoll(() => refresh(), live || sending ? 2500 : 8000, threadId != null);

  const send = useCallback(
    async (raw: string, extra?: Record<string, unknown>, options: { stepUp?: { title: string; subtitle: string } } = {}): Promise<"sent" | "cancelled" | "skipped"> => {
      const content = raw.trim();
      if (!content || sendingRef.current) return "skipped";
      sendingRef.current = true;
      setSending(true);
      const userId = clientId();
      const loadingId = clientId();
      const controller = new AbortController();
      let timedOut = false;
      const timeout = setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, CHAT_SEND_TIMEOUT_MS);
      setMessages((prev) => [...prev, optimistic(userId, "user", content, "sending"), optimistic(loadingId, "assistant", "", "loading", content)]);
      try {
        let tid = threadRef.current;
        if (!tid) {
          tid = (await api.createThread()).id;
          threadRef.current = tid;
          setThreadId(tid);
        }
        await api.streamChatMessage(
          tid,
          content,
          { source: "user_mode_chat", view_mode: "user", ...(extra || {}) },
          {
            onMeta: (meta) => {
              setMessages((prev) => prev.map((m) => (m.id === userId ? meta.user_message : m)));
              setQuestion(meta.pending_question ?? null);
              const started = Number(
                (meta.task_request as { run_id?: number } | null)?.run_id ?? (meta.application_request as { run_id?: number } | null)?.run_id ?? 0
              );
              if (started) runIdRef.current = started;
            },
            onToken: (delta) => setMessages((prev) => prev.map((m) => (m.id === loadingId ? { ...m, content: m.content + delta } : m))),
            onDone: (done) => {
              const doneRun = Number((done.chat_response as { active_run_id?: number } | null)?.active_run_id ?? 0);
              if (doneRun) runIdRef.current = doneRun;
              setMessages((prev) => prev.map((m) => (m.id === loadingId ? done.assistant_message : m)));
            },
            signal: controller.signal
          },
          options.stepUp
        );
        sendingRef.current = false;
        await refresh({ messages: true }).catch(() => {});
        return "sent";
      } catch (error) {
        if (error instanceof StepUpCancelled) {
          setMessages((prev) => prev.filter((m) => m.id !== userId && m.id !== loadingId));
          return "cancelled";
        } else {
          const text = timedOut ? "Lumen took too long to answer. You can send again." : errorMessage(error, "Lumen couldn't answer. Try again.");
          setMessages((prev) => prev.map((m) => (m.id === loadingId ? optimistic(loadingId, "assistant", text, "error", content) : m)));
          return "sent";
        }
      } finally {
        clearTimeout(timeout);
        sendingRef.current = false;
        setSending(false);
      }
    },
    [refresh]
  );

  const retry = useCallback(
    (message: ChatMessage) => {
      const retryContent = message.payload_json?.retry_content;
      if (typeof retryContent === "string" && retryContent.trim()) {
        setMessages((prev) => prev.filter((m) => m.id !== message.id && !(m.role === "user" && m.payload_json?.client_status === "sending")));
        void send(retryContent);
      }
    },
    [send]
  );

  const answerFinalAction = useCallback(
    async (target: OperatorQuestion, contract: FinalActionContract, choice: { label: string; decision: "approve" | "decline" }) => {
      setSending(true);
      sendingRef.current = true;
      try {
        await api.answerFinalAction(
          target.id,
          { answer: choice.label, approvalToken: contract.approvalToken, decision: choice.decision },
          contract.paymentFloor
            ? { title: "Confirm the payment", subtitle: contract.summary || `Lumen will click “${contract.submitLabel || choice.label}”.` }
            : { title: "Approve the final step", subtitle: `Lumen will click “${contract.submitLabel || choice.label}”.` },
          contract.paymentFloor
        );
      } finally {
        sendingRef.current = false;
        setSending(false);
        await refresh({ messages: true }).catch(() => {});
      }
    },
    [refresh]
  );

  const control = useCallback(
    async (action: "pause" | "resume" | "cancel") => {
      const id = runIdRef.current;
      if (!id) return;
      const next = await api[action](id);
      setRun(next);
      await refresh().catch(() => {});
    },
    [refresh]
  );

  return { threadId, thread, messages, run, question, sending, loaded, refresh, send, retry, answerFinalAction, control };
}

export function isApiError(error: unknown, status: number): boolean {
  return error instanceof ApiError && error.status === status;
}
