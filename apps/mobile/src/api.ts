import * as Crypto from "expo-crypto";
import { File, Paths } from "expo-file-system";
import { fetch } from "expo/fetch";
import type {
  AgentSettings,
  ApplicationArtifact,
  ApplicationArtifactPreview,
  ChatMessage,
  ChatStreamMeta,
  ChatThread,
  CredentialVaultFields,
  CredentialVaultItem,
  ModelProvider,
  OperatorQuestion,
  OperatorRun,
  ProfilePayload,
  QuestionArtifactResult
} from "@/lib/api";
import type { MemoryPayload } from "@/lib/memoryFacts";
import LumenKeys from "../modules/lumen-keys/src/LumenKeysModule";
import { STEP_UP_KEY_ALIAS, type Session } from "./session";
import { stepUpMessage } from "./stepUpMessage";

export type {
  AgentSettings,
  ApplicationArtifact,
  ApplicationArtifactPreview,
  ChatMessage,
  ChatStreamMeta,
  ChatThread,
  CredentialVaultFields,
  CredentialVaultItem,
  ModelProvider,
  OperatorQuestion,
  OperatorRun,
  ProfilePayload,
  QuestionArtifactResult
};

export type PairedDevice = {
  id: number;
  name: string;
  platform: string;
  has_push: boolean;
  /** Until when tasks this phone starts may use Full autonomy (one fingerprint unlocks it). */
  autonomy_until: string | null;
  last_seen_at: string | null;
  created_at: string;
};

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly stepUpRequired = false
  ) {
    super(message);
  }
}

/** The user dismissed the fingerprint prompt; callers stay quiet about it. */
export class StepUpCancelled extends Error {
  constructor() {
    super("Cancelled.");
  }
}

let session: Session | null = null;
let onUnpaired: (() => void) | null = null;

export function bindSession(next: Session | null, unpaired: (() => void) | null): void {
  session = next;
  onUnpaired = unpaired;
}

function requireSession(): Session {
  if (!session) throw new ApiError("This phone isn't paired with Lumen.", 401);
  return session;
}

/** What the fingerprint prompt says the user is confirming. */
export type StepUpReason = { title: string; subtitle: string };

type Body = { json?: unknown; form?: FormData };

type CallOptions = Body & {
  /** "always": sign before sending. "on-demand": sign and retry once if the server asks. */
  stepUp?: "always" | "on-demand" | "never";
  reason?: StepUpReason;
  timeoutMs?: number;
};

const DEFAULT_REASON: StepUpReason = { title: "Confirm with Lumen", subtitle: "Lumen on your PC asked this phone to confirm." };

function detailOf(text: string, status: number): string {
  try {
    const parsed = JSON.parse(text) as { detail?: unknown };
    if (typeof parsed.detail === "string" && parsed.detail.trim()) return parsed.detail.trim();
    if (Array.isArray(parsed.detail)) return "Lumen couldn't read that request.";
  } catch {
    // Not JSON.
  }
  return text.trim() || `Lumen returned ${status}.`;
}

async function stepUpHeader(method: string, target: string, bodyText: string, reason: StepUpReason): Promise<string> {
  const challenge = await call<{ nonce: string; format: string }>("POST", "/devices/me/challenges", { json: {}, stepUp: "never" });
  const bodySha = await Crypto.digestStringAsync(Crypto.CryptoDigestAlgorithm.SHA256, bodyText);
  const message = stepUpMessage(challenge.nonce, method, target, bodySha);
  try {
    const signature = await LumenKeys.sign(STEP_UP_KEY_ALIAS, message, reason.title, reason.subtitle);
    return `${challenge.nonce}.${signature}`;
  } catch (error) {
    const code = (error as { code?: string }).code;
    if (code === "CANCELLED") throw new StepUpCancelled();
    if (code === "KEY_INVALIDATED" || code === "NO_KEY") {
      throw new ApiError("This phone's fingerprint key changed. Remove it on your PC and pair again.", 401, true);
    }
    throw new ApiError((error as Error).message || "The fingerprint check failed.", 401, true);
  }
}

async function call<T>(method: string, target: string, options: CallOptions = {}): Promise<T> {
  const current = requireSession();
  const bodyText = options.json !== undefined ? JSON.stringify(options.json) : "";
  const mode = options.stepUp ?? "on-demand";

  const send = async (assertion: string | null) => {
    const headers: Record<string, string> = { Authorization: `Bearer ${current.token}`, Accept: "application/json" };
    if (options.json !== undefined) headers["Content-Type"] = "application/json";
    if (assertion) headers["X-Lumen-Step-Up"] = assertion;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), options.timeoutMs ?? 30000);
    try {
      return await fetch(`${current.baseUrl}${target}`, {
        method,
        headers,
        body: options.form ?? (options.json !== undefined ? bodyText : undefined),
        signal: controller.signal
      });
    } catch (error) {
      if (controller.signal.aborted) throw new ApiError("Lumen took too long to answer.", 0);
      throw new ApiError("Can't reach Lumen. Check that your PC is on and Tailscale is connected.", 0);
    } finally {
      clearTimeout(timeout);
    }
  };

  const reason = options.reason ?? DEFAULT_REASON;
  let response = await send(mode === "always" ? await stepUpHeader(method, target, bodyText, reason) : null);
  if (mode === "on-demand" && response.status === 403 && response.headers.get("x-lumen-step-up-required") === "1" && !options.form) {
    response = await send(await stepUpHeader(method, target, bodyText, reason));
  }
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    const stepUp = response.headers.get("x-lumen-step-up-required") === "1";
    if (response.status === 401 && !stepUp) onUnpaired?.();
    throw new ApiError(detailOf(text, response.status), response.status, stepUp);
  }
  return (await response.json()) as T;
}

// -------------------------------------------------------------------------------------------------
// Chat stream (Server-Sent Events: one `meta`, `token`s, then `done`)

type ChatStreamDone = { assistant_message: ChatMessage; context: Record<string, unknown>; chat_response: Record<string, unknown> | null };

export type ChatStreamHandlers = {
  onMeta?: (meta: ChatStreamMeta) => void;
  onToken?: (text: string) => void;
  onDone?: (done: ChatStreamDone) => void;
  signal?: AbortSignal;
};

async function streamChatMessage(
  threadId: number,
  content: string,
  payload: Record<string, unknown>,
  handlers: ChatStreamHandlers,
  stepUp?: StepUpReason
): Promise<void> {
  const current = requireSession();
  const target = `/operator/chat/threads/${threadId}/messages`;
  const bodyText = JSON.stringify({ content, payload });
  const headers: Record<string, string> = {
    Authorization: `Bearer ${current.token}`,
    "Content-Type": "application/json",
    Accept: "text/event-stream"
  };
  if (stepUp) headers["X-Lumen-Step-Up"] = await stepUpHeader("POST", target, bodyText, stepUp);
  let response;
  try {
    response = await fetch(`${current.baseUrl}${target}`, { method: "POST", headers, body: bodyText, signal: handlers.signal });
  } catch {
    if (handlers.signal?.aborted) throw new ApiError("Lumen took too long to answer.", 0);
    throw new ApiError("Can't reach Lumen. Check that your PC is on and Tailscale is connected.", 0);
  }
  if (!response.ok || !response.body) {
    const text = await response.text().catch(() => "");
    if (response.status === 401) onUnpaired?.();
    throw new ApiError(detailOf(text, response.status), response.status);
  }
  const dispatch = (frame: string) => {
    let event = "";
    const dataLines: string[] = [];
    for (const line of frame.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
    }
    if (!event || !dataLines.length) return;
    const parsed = JSON.parse(dataLines.join("\n"));
    if (event === "meta") handlers.onMeta?.(parsed as ChatStreamMeta);
    else if (event === "token") handlers.onToken?.(String(parsed.text ?? ""));
    else if (event === "done") handlers.onDone?.(parsed as ChatStreamDone);
    else if (event === "error") throw new ApiError(typeof parsed.detail === "string" ? parsed.detail : "Lumen chat stream failed.", 500);
  };
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      if (frame.trim()) dispatch(frame);
      boundary = buffer.indexOf("\n\n");
    }
  }
  if (buffer.trim()) dispatch(buffer.trim());
}

// -------------------------------------------------------------------------------------------------
// Pairing (no session yet)

export async function pairDevice(baseUrl: string, payload: { code: string; name: string; public_key: string }): Promise<{ device: PairedDevice; token: string }> {
  let response;
  try {
    response = await fetch(`${baseUrl}/devices/pair`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ ...payload, platform: "android" })
    });
  } catch {
    throw new ApiError("Can't reach your PC. Is Tailscale connected on this phone?", 0);
  }
  const text = await response.text();
  if (!response.ok) throw new ApiError(detailOf(text, response.status), response.status);
  return JSON.parse(text) as { device: PairedDevice; token: string };
}

// -------------------------------------------------------------------------------------------------

type QuestionAnswerResult = { question: OperatorQuestion; run: OperatorRun; applied: Record<string, unknown> | null; can_resume?: boolean | null };


type PickedFile = { uri: string; name: string; mimeType?: string | null };

function fileForm(picked: PickedFile, extra: Record<string, string> = {}): FormData {
  const form = new FormData();
  form.append("file", new File(picked.uri) as unknown as Blob, picked.name);
  for (const [key, value] of Object.entries(extra)) form.append(key, value);
  return form;
}

export const api = {
  me: () => call<{ device: PairedDevice; autonomy_hours: number }>("GET", "/devices/me"),
  unlockAutonomy: (hours: number) =>
    call<{ device: PairedDevice }>("POST", "/devices/me/autonomy", {
      json: {},
      stepUp: "always",
      reason: {
        title: "Use Full autonomy from this phone",
        subtitle: `For the next ${hours} hours, tasks you start here can finish purchases and other final actions without asking.`
      }
    }),
  lockAutonomy: () => call<{ device: PairedDevice }>("DELETE", "/devices/me/autonomy"),
  unpair: () => call<{ revoked: boolean }>("DELETE", "/devices/me"),
  registerPushToken: (pushToken: string | null) => call<{ device: PairedDevice }>("POST", "/devices/me/push-token", { json: { push_token: pushToken } }),

  runs: () => call<OperatorRun[]>("GET", "/operator/runs"),
  run: (id: number) => call<OperatorRun>("GET", `/operator/runs/${id}`),
  runQuestions: (id: number) => call<OperatorQuestion[]>("GET", `/operator/runs/${id}/questions`),
  pause: (id: number) => call<OperatorRun>("POST", `/operator/runs/${id}/pause`, { json: {} }),
  resume: (id: number) => call<OperatorRun>("POST", `/operator/runs/${id}/resume`, { json: {} }),
  cancel: (id: number) => call<OperatorRun>("POST", `/operator/runs/${id}/cancel`, { json: {} }),
  sensitiveContinue: (id: number, handoffId: string) =>
    call<OperatorRun>("POST", `/operator/runs/${id}/sensitive-browser-continue`, { json: { action: "sensitive_browser_continue", handoff_id: handoffId } }),

  threads: () => call<ChatThread[]>("GET", "/operator/chat/threads"),
  createThread: () => call<ChatThread>("POST", "/operator/chat/threads", { json: {} }),
  deleteThread: (id: number) => call<ChatThread>("DELETE", `/operator/chat/threads/${id}`),
  messages: (threadId: number) => call<ChatMessage[]>("GET", `/operator/chat/threads/${threadId}/messages`),
  streamChatMessage,

  // A payment approval signs up front; any other approval signs only if the PC asks (its verdict wins).
  answerFinalAction: (
    questionId: number,
    payload: { answer: string; approvalToken: string; decision: "approve" | "decline" },
    reason: StepUpReason,
    payment: boolean
  ) =>
    call<QuestionAnswerResult>("POST", `/operator/questions/${questionId}/answer`, {
      json: {
        answer: payload.answer,
        payload: {},
        auto_resume: true,
        save_scope: "one_time",
        approval_token: payload.approvalToken,
        final_action_decision: payload.decision
      },
      stepUp: payment && payload.decision === "approve" ? "always" : "on-demand",
      reason
    }),

  artifacts: () => call<ApplicationArtifact[]>("GET", "/artifacts"),
  previewArtifact: (file: PickedFile) => call<ApplicationArtifactPreview>("POST", "/artifacts/preview", { form: fileForm(file), timeoutMs: 120000 }),
  commitArtifact: (payload: { draftId: string; selectedType: string; displayName?: string }) => {
    const form = new FormData();
    form.append("draft_id", payload.draftId);
    form.append("selected_type", payload.selectedType);
    if (payload.displayName?.trim()) form.append("display_name", payload.displayName.trim());
    return call<ApplicationArtifact>("POST", "/artifacts", { form, timeoutMs: 120000 });
  },
  updateArtifact: (id: number, textContent: string) => call<ApplicationArtifact>("PATCH", `/artifacts/${id}`, { json: { text_content: textContent } }),
  deleteArtifact: (id: number) => call<{ deleted: boolean }>("DELETE", `/artifacts/${id}`),
  downloadArtifact: async (artifact: Pick<ApplicationArtifact, "id" | "filename">): Promise<File> => {
    const current = requireSession();
    const safeName = artifact.filename.replace(/[^\w.\- ]+/g, "_") || `document-${artifact.id}`;
    const destination = new File(Paths.cache, safeName);
    if (destination.exists) destination.delete();
    const task = File.createDownloadTask(`${current.baseUrl}/artifacts/${artifact.id}/download`, destination, {
      headers: { Authorization: `Bearer ${current.token}` }
    });
    const result = await task.downloadAsync();
    if (!result) throw new ApiError("The download didn't finish.", 0);
    return result;
  },
  uploadQuestionArtifact: (questionId: number, file: PickedFile) =>
    call<QuestionArtifactResult>("POST", `/operator/questions/${questionId}/artifacts/upload?auto_resume=true`, { form: fileForm(file), timeoutMs: 120000 }),
  generateQuestionArtifact: (questionId: number, payload: { instructions?: string; artifact_kind?: string }) =>
    call<QuestionArtifactResult>("POST", `/operator/questions/${questionId}/artifacts/generate`, { json: payload, timeoutMs: 120000 }),
  applyQuestionArtifact: (questionId: number, artifactId: number) =>
    call<QuestionArtifactResult>("POST", `/operator/questions/${questionId}/artifacts/${artifactId}/apply`, { json: { auto_resume: true } }),

  profile: () => call<ProfilePayload>("GET", "/profiles/me"),
  memory: () => call<MemoryPayload>("GET", "/memory"),
  addMemoryFact: (payload: { label: string; value: string; section?: string }) => call<MemoryPayload>("POST", "/memory", { json: payload }),
  updateMemoryFact: (key: string, payload: { label?: string; value?: string; section?: string }) =>
    call<MemoryPayload>("PATCH", `/memory/${encodeURIComponent(key)}`, { json: payload }),
  deleteMemoryFact: (key: string) => call<MemoryPayload>("DELETE", `/memory/${encodeURIComponent(key)}`),

  agentSettings: () => call<{ settings: AgentSettings }>("GET", "/agent-settings"),
  updateAgentSettings: (patch: Partial<AgentSettings>, reason: StepUpReason) =>
    call<{ settings: AgentSettings }>("PATCH", "/agent-settings", { json: patch, stepUp: "always", reason }),
  modelProviders: () => call<{ items: ModelProvider[]; active: ModelProvider }>("GET", "/model-providers"),

  credentialVault: (origin?: string) =>
    call<{ items: CredentialVaultItem[] }>("GET", `/credential-vault${origin ? `?origin=${encodeURIComponent(origin)}` : ""}`),
  createVaultItem: (payload: { origin: string; account_label: string; fields: CredentialVaultFields; auto_use: boolean }) =>
    call<CredentialVaultItem>("POST", "/credential-vault", {
      json: payload,
      stepUp: "always",
      reason: { title: "Save this sign-in", subtitle: `Lumen stores it encrypted on your PC for ${payload.origin}.` }
    }),
  updateVaultItem: (id: number, payload: { account_label?: string; fields?: CredentialVaultFields; auto_use?: boolean }) =>
    call<CredentialVaultItem>("PATCH", `/credential-vault/${id}`, {
      json: payload,
      stepUp: "always",
      reason: { title: "Update this sign-in", subtitle: "Lumen stores it encrypted on your PC." }
    }),
  deleteVaultItem: (id: number) =>
    call<{ deleted: boolean }>("DELETE", `/credential-vault/${id}`, {
      stepUp: "always",
      reason: { title: "Remove this sign-in", subtitle: "Lumen won't be able to use it any more." }
    }),

  /** expo-image source for a run's latest frame, exactly as the model saw it. */
  latestScreenshot: (runId: number, cacheBust: string) => {
    const current = requireSession();
    return {
      uri: `${current.baseUrl}/operator/runs/${runId}/screenshot/latest?v=${encodeURIComponent(cacheBust)}`,
      headers: { Authorization: `Bearer ${current.token}` }
    };
  }
};
