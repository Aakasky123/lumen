import type { MemoryPayload } from "@/lib/memoryFacts";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "/api/backend";

type SensitiveBrowserTakeoverState = {
  active: boolean;
  handoff_id?: string;
  status?: string;
  persist_answer?: boolean;
  kind?: string;
  entry_surface?: string;
  credential_origin?: string;
  credential_slot?: string;
};

type OperatorConfidenceState = Record<string, unknown> & {
  sensitive_browser_takeover?: SensitiveBrowserTakeoverState;
};

export type OperatorRun = {
  id: number;
  application_id: number | null;
  user_id: number;
  goal: string;
  target_url: string;
  current_state: string;
  active_plan: Array<{ id: string; label: string; status: string }>;
  confidence_state: OperatorConfidenceState;
  risk_level: string;
  cost_estimate: number;
  status_message: string;
  created_at: string;
  updated_at: string;
};

export type ChatThread = {
  id: number;
  user_id: number;
  run_id: number | null;
  title: string;
  mode: string;
  status: string;
  created_at: string;
  updated_at: string;
  /** Model spend in USD recorded for the thread's run, updated while it works; null without a run. */
  cost_usd?: number | null;
};

export type ChatMessage = {
  id: number;
  thread_id: number;
  role: string;
  content: string;
  payload_json: Record<string, unknown>;
  linked_event_id: number | null;
  linked_task_id: number | null;
  created_at: string;
};

export type OperatorQuestion = {
  id: number;
  run_id: number;
  thread_id: number | null;
  question_type: string;
  stuck_phase: string;
  stuck_reason: string;
  prompt: string;
  expected_answer_schema: Record<string, unknown>;
  status: string;
  answer_payload: Record<string, unknown>;
  applied_at: string | null;
  resumed_at: string | null;
  created_at: string;
  updated_at: string;
};

export type ProfilePayload = {
  user: { id: number; email: string; name: string };
  profile: {
    phone: string | null;
    location: string | null;
    linkedin: string | null;
    github: string | null;
    portfolio: string | null;
    work_authorization: string | null;
    sponsorship_status: string | null;
    salary_expectation: string | null;
    relocation_preference: string | null;
    profile_json: Record<string, unknown>;
  } | null;
  common_answers: Array<{
    id: number;
    question_pattern: string;
    answer: string;
    confidence: number;
    last_used_at: string | null;
  }>;
};

type ProfileUpsertPayload = {
  name: string;
  email: string;
  phone?: string | null;
  location?: string | null;
  linkedin?: string | null;
  github?: string | null;
  portfolio?: string | null;
  work_authorization?: string | null;
  sponsorship_status?: string | null;
  salary_expectation?: string | null;
  relocation_preference?: string | null;
  profile_json?: Record<string, unknown>;
};

export type ClosedWorldAttestation = {
  id: string;
  scope: string;
  set_key: string;
  members: string[];
  exhaustive: boolean;
  status: "active" | "revoked";
  attested_by: "user";
  source: "profile_attestation_api";
  version: number;
  created_at: string;
  updated_at: string;
  revoked_at: string | null;
};

type ClosedWorldAttestationCreateInput = {
  scope: string;
  set_key: string;
  members: string[];
};

export type CredentialVaultItem = {
  id: number;
  account_label: string;
  origin: string;
  available_slots: string[];
  auto_use: boolean;
  created_at: string;
  updated_at: string;
  last_used_at: string | null;
};

export type CredentialVaultFields = {
  username?: string;
  email?: string;
  password?: string;
};

type CredentialVaultCreateInput = {
  origin: string;
  account_label: string;
  fields: CredentialVaultFields;
  auto_use: boolean;
};

type CredentialVaultUpdateInput = Partial<Omit<CredentialVaultCreateInput, "fields">> & {
  fields?: CredentialVaultFields;
};

export type ModelProviderType = "openai" | "deepseek" | "openrouter" | "anthropic" | "gemini" | "custom_openai_compatible";

type AvailableModelsInput = {
  provider_type?: ModelProviderType | string;
  api_key?: string;
  base_url?: string;
  /** Use a saved provider's key on the same service; the key stays on the server. */
  key_from_provider_id?: number;
};

export type ModelProvider = {
  id: number | null;
  display_name: string;
  provider_type: ModelProviderType | string;
  model_name: string;
  base_url: string;
  api_key_masked: string;
  has_api_key: boolean;
  temperature: number;
  max_tokens: number;
  timeout_seconds: number;
  enabled: boolean;
  is_active: boolean;
  source: string;
  health_status: string;
  last_error: string | null;
  last_tested_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

type ModelProviderInput = {
  display_name: string;
  provider_type: ModelProviderType | string;
  model_name: string;
  base_url?: string;
  api_key?: string;
  /** Use a saved provider's key on the same service; the key stays on the server. */
  key_from_provider_id?: number;
  temperature: number;
  max_tokens: number;
  timeout_seconds: number;
  enabled: boolean;
};

type ModelProvidersResponse = {
  items: ModelProvider[];
  active: ModelProvider;
  env_fallback: ModelProvider;
  supported_provider_types: ModelProviderType[];
};

export type ApplicationArtifact = {
  id: number;
  user_id: number;
  run_id: number | null;
  application_id: number | null;
  job_posting_id: number | null;
  artifact_kind: string;
  source: string;
  filename: string;
  mime_type: string;
  storage_path: string | null;
  text_content: string;
  text_preview: string;
  status: string;
  metadata_json: Record<string, unknown>;
  file_size: number;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
};

export type ApplicationArtifactPreview = {
  draft_id: string;
  filename: string;
  mime_type: string;
  file_size: number;
  text_preview: string;
  extracted_text_preview: string;
  suggested_artifact_kind: string;
  suggested_display_name: string;
  summary: string;
  key_facts: Array<{ label: string; value: string; evidence?: string }>;
  confidence: number;
  warnings: string[];
  extraction_status: string;
  provider: string | null;
  model: string | null;
};

export type QuestionArtifactResult = {
  artifact: ApplicationArtifact | null;
  question: OperatorQuestion | null;
  run: OperatorRun | null;
  applied: Record<string, unknown> | null;
};

type QuestionAnswerResult = {
  question: OperatorQuestion;
  run: OperatorRun;
  applied: Record<string, unknown> | null;
  can_resume?: boolean | null;
};

async function send<T>(path: string, init: RequestInit, timeoutMs?: number): Promise<T> {
  const controller = timeoutMs ? new AbortController() : null;
  const timeout = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      ...init,
      signal: controller?.signal,
      cache: "no-store"
    });
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || `Request failed: ${response.status}`);
    }
    return response.json() as Promise<T>;
  } catch (err) {
    if (controller?.signal.aborted) {
      throw new Error("Lumen took too long to respond. Please try again.");
    }
    throw err;
  } finally {
    if (timeout) clearTimeout(timeout);
  }
}

function request<T>(
  path: string,
  { timeoutMs, headers, ...init }: Omit<RequestInit, "headers"> & { headers?: Record<string, string>; timeoutMs?: number } = {}
): Promise<T> {
  return send<T>(path, { ...init, headers: { "Content-Type": "application/json", ...headers } }, timeoutMs);
}

function requestForm<T>(path: string, body: FormData, timeoutMs?: number): Promise<T> {
  return send<T>(path, { method: "POST", body }, timeoutMs);
}

export type ChatStreamMeta = {
  user_message: ChatMessage;
  chat_scope: Record<string, unknown> | null;
  pending_question: OperatorQuestion | null;
  task_request: Record<string, unknown> | null;
  application_request: Record<string, unknown> | null;
  run_control: Record<string, unknown> | null;
  runtime_correction: Record<string, unknown> | null;
  question_answer: Record<string, unknown> | null;
};

type ChatStreamDone = {
  assistant_message: ChatMessage;
  context: Record<string, unknown>;
  chat_response: Record<string, unknown> | null;
};

type ChatStreamHandlers = {
  onMeta?: (meta: ChatStreamMeta) => void;
  onToken?: (text: string) => void;
  onDone?: (done: ChatStreamDone) => void;
  signal?: AbortSignal;
};

export type AgentSettings = {
  /** The Brain: decides every step and answers chat. Setting it also makes the row the active provider. */
  vision_provider_id: number | null;
  /** Escalation: a smarter model that takes over a part of a task the Brain keeps failing. Never the Brain or the Checker. */
  vision_escalation_provider_id?: number | null;
  /** The Checker: the quick checks around each action, the second opinion on secrets, and the backup when the
   * Brain's provider fails. Never the Brain's model. */
  judge_provider_id?: number | null;
  strict_model_provider_allowlist: boolean;
  final_action_mode: "approval" | "autonomous";
  autonomous_attestation_consent?: boolean;
  autonomous_relay_consent?: boolean;
};

const LOCAL_ADMIN = { "X-Lumen-Local": "1" };

export type PairedDevice = {
  id: number;
  name: string;
  platform: string;
  has_push: boolean;
  last_seen_at: string | null;
  created_at: string;
};

export type RemoteAccessStatus = {
  ready: boolean;
  problem: string | null;
  public_url: string | null;
  listener: { enabled: boolean; port: number; running: boolean; error: string | null };
  tailscale: { installed: boolean; logged_in: boolean; dns_name: string | null; url: string | null; serve_proxy: string | null };
  serve_command: string;
  push: { configured: boolean; project_id: string | null };
};

export type DevicePairingCode = {
  code: string;
  expires_at: string;
  expires_in_seconds: number;
  public_url: string | null;
  pairing_uri: string | null;
  ready: boolean;
  problem: string | null;
};

export const api = {
  modelProviders: () => request<ModelProvidersResponse>("/model-providers"),
  createModelProvider: (payload: ModelProviderInput) =>
    request<{ provider: ModelProvider; active: ModelProvider }>("/model-providers", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  activateModelProvider: (id: number) =>
    request<{ provider: ModelProvider; active: ModelProvider }>(`/model-providers/${id}/activate`, {
      method: "POST",
      body: JSON.stringify({})
    }),
  availableModels: (payload: AvailableModelsInput) =>
    request<{ models: string[] }>("/model-providers/available-models", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  agentSettings: () => request<{ settings: AgentSettings }>("/agent-settings"),
  updateAgentSettings: (payload: Partial<AgentSettings>) =>
    request<{ settings: AgentSettings }>("/agent-settings", {
      method: "PATCH",
      body: JSON.stringify(payload)
    }),
  runs: () => request<OperatorRun[]>("/operator/runs"),
  // Device management is local-only; the backend also requires this header on those routes.
  remoteAccessStatus: (refresh = false) =>
    request<RemoteAccessStatus>(`/devices/remote-status${refresh ? "?refresh=true" : ""}`, { headers: LOCAL_ADMIN, timeoutMs: 15000 }),
  pairedDevices: () => request<{ devices: PairedDevice[] }>("/devices", { headers: LOCAL_ADMIN }),
  createPairingCode: () =>
    request<DevicePairingCode>("/devices/pairing-codes", { method: "POST", body: JSON.stringify({}), headers: LOCAL_ADMIN, timeoutMs: 15000 }),
  revokePairedDevice: (id: number) => request<{ revoked: boolean; id: number }>(`/devices/${id}`, { method: "DELETE", headers: LOCAL_ADMIN }),
  profile: () => request<ProfilePayload>("/profiles/me"),
  memory: () => request<MemoryPayload>("/memory"),
  addMemoryFact: (payload: { label: string; value: string; section?: string }) =>
    request<MemoryPayload>("/memory", { method: "POST", body: JSON.stringify(payload) }),
  updateMemoryFact: (key: string, payload: { label?: string; value?: string; section?: string }) =>
    request<MemoryPayload>(`/memory/${encodeURIComponent(key)}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteMemoryFact: (key: string) => request<MemoryPayload>(`/memory/${encodeURIComponent(key)}`, { method: "DELETE" }),
  upsertProfile: (payload: ProfileUpsertPayload) =>
    request<{ user_id: number; profile_id: number; status: string }>("/profiles", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  closedWorldAttestations: () =>
    request<{ items: ClosedWorldAttestation[] }>("/profiles/me/closed-world-attestations"),
  createClosedWorldAttestation: (payload: ClosedWorldAttestationCreateInput) =>
    request<{ attestation: ClosedWorldAttestation }>("/profiles/me/closed-world-attestations", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  revokeClosedWorldAttestation: (attestationId: string) =>
    request<{ attestation: ClosedWorldAttestation }>(`/profiles/me/closed-world-attestations/${attestationId}/revoke`, {
      method: "POST",
      body: JSON.stringify({})
    }),
  credentialVault: (origin?: string) =>
    request<{ items: CredentialVaultItem[] }>(
      `/credential-vault${origin ? `?origin=${encodeURIComponent(origin)}` : ""}`
    ),
  createCredentialVaultItem: (payload: CredentialVaultCreateInput) =>
    request<CredentialVaultItem>("/credential-vault", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  updateCredentialVaultItem: (id: number, payload: CredentialVaultUpdateInput) =>
    request<CredentialVaultItem>(`/credential-vault/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload)
    }),
  deleteCredentialVaultItem: (id: number) =>
    request<{ deleted: boolean; id: number }>(`/credential-vault/${id}`, {
      method: "DELETE"
    }),
  updateAnswer: (id: number, answer: string) =>
    request<{ id: number; answer: string }>(`/operator/answers/${id}`, { method: "PATCH", body: JSON.stringify({ answer }) }),
  deleteAnswer: (id: number) => request<{ deleted: boolean; id: number }>(`/operator/answers/${id}`, { method: "DELETE" }),
  getRun: (id: number) => request<OperatorRun>(`/operator/runs/${id}`),
  answerFinalAction: (
    questionId: number,
    payload: { answer: string; approvalToken: string; decision: "approve" | "decline" }
  ) =>
    request<QuestionAnswerResult>(`/operator/questions/${questionId}/answer`, {
      method: "POST",
      body: JSON.stringify({
        answer: payload.answer,
        payload: {},
        auto_resume: true,
        save_scope: "one_time",
        approval_token: payload.approvalToken,
        final_action_decision: payload.decision
      })
    }),
  pause: (id: number) =>
    request<OperatorRun>(`/operator/runs/${id}/pause`, {
      method: "POST",
      body: JSON.stringify({}),
      timeoutMs: 15000
    }),
  resume: (id: number) =>
    request<OperatorRun>(`/operator/runs/${id}/resume`, {
      method: "POST",
      body: JSON.stringify({}),
      timeoutMs: 15000
    }),
  sensitiveBrowserContinue: (id: number, handoffId: string) =>
    request<OperatorRun>(`/operator/runs/${id}/sensitive-browser-continue`, {
      method: "POST",
      body: JSON.stringify({ action: "sensitive_browser_continue", handoff_id: handoffId }),
      timeoutMs: 15000
    }),
  cancel: (id: number) =>
    request<OperatorRun>(`/operator/runs/${id}/cancel`, {
      method: "POST",
      body: JSON.stringify({}),
      timeoutMs: 15000
    }),
  applicationArtifacts: () => request<ApplicationArtifact[]>("/artifacts"),
  previewApplicationArtifact: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return requestForm<ApplicationArtifactPreview>("/artifacts/preview", form, 120000);
  },
  commitApplicationArtifact: (payload: { draftId: string; selectedType: string; displayName?: string }) => {
    const form = new FormData();
    form.append("draft_id", payload.draftId);
    form.append("selected_type", payload.selectedType);
    if (payload.displayName?.trim()) form.append("display_name", payload.displayName.trim());
    return requestForm<ApplicationArtifact>("/artifacts", form, 120000);
  },
  updateApplicationArtifact: (artifactId: number, textContent: string) =>
    request<ApplicationArtifact>(`/artifacts/${artifactId}`, {
      method: "PATCH",
      body: JSON.stringify({ text_content: textContent })
    }),
  deleteApplicationArtifact: (artifactId: number) =>
    request<{ deleted: boolean; id: number; deleted_files: string[] }>(`/artifacts/${artifactId}`, {
      method: "DELETE"
    }),
  applicationArtifactDownloadUrl: (artifactId: number) => `${API_BASE}/artifacts/${artifactId}/download`,
  uploadQuestionArtifact: (questionId: number, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return requestForm<QuestionArtifactResult>(`/operator/questions/${questionId}/artifacts/upload?auto_resume=true`, form);
  },
  generateQuestionArtifact: (questionId: number, payload: { instructions?: string; artifact_kind?: string }) =>
    request<QuestionArtifactResult>(`/operator/questions/${questionId}/artifacts/generate`, {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  applyQuestionArtifact: (questionId: number, artifactId: number) =>
    request<QuestionArtifactResult>(`/operator/questions/${questionId}/artifacts/${artifactId}/apply`, {
      method: "POST",
      body: JSON.stringify({ auto_resume: true })
    }),
  chatThreads: (runId?: number) => request<ChatThread[]>(`/operator/chat/threads${runId ? `?run_id=${runId}` : ""}`),
  createChatThread: () =>
    request<ChatThread>("/operator/chat/threads", {
      method: "POST",
      body: JSON.stringify({})
    }),
  deleteChatThread: (id: number) =>
    request<ChatThread>(`/operator/chat/threads/${id}`, {
      method: "DELETE"
    }),
  chatMessages: (threadId: number) => request<ChatMessage[]>(`/operator/chat/threads/${threadId}/messages`),
  streamChatMessage: async (
    threadId: number,
    content: string,
    payload: Record<string, unknown>,
    handlers: ChatStreamHandlers
  ): Promise<void> => {
    const response = await fetch(`${API_BASE}/operator/chat/threads/${threadId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ content, payload }),
      cache: "no-store",
      signal: handlers.signal
    });
    if (!response.ok || !response.body) {
      const detail = await response.text().catch(() => "");
      throw new Error(detail || `Request failed: ${response.status}`);
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
      else if (event === "error") throw new Error(typeof parsed.detail === "string" ? parsed.detail : "Lumen chat stream failed.");
    };
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        if (frame.trim()) dispatch(frame);
        boundary = buffer.indexOf("\n\n");
      }
    }
    const tail = buffer.trim();
    if (tail) dispatch(tail);
  },
  runQuestions: (runId: number) => request<OperatorQuestion[]>(`/operator/runs/${runId}/questions`)
};
