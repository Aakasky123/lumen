import type { OperatorQuestion } from "@/lib/api";
import { isMultipleChoiceField } from "@/lib/pendingQuestionConfirmation";

/**
 * How a pending question's contract reads, ported from the cockpit's PendingQuestionCard so the
 * phone renders and answers each question exactly the way the PC does. The confirmation helpers
 * themselves are shared directly (apps/renderer/lib/pendingQuestionConfirmation.ts).
 */
export type FieldPrompt = Record<string, unknown>;

export type ArtifactOption = { id: number; name: string; preview: string };

export type FinalActionContract = {
  approvalToken: string;
  choices: Array<{ label: string; decision: "approve" | "decline" }>;
  submitLabel: string;
  paymentFloor: boolean;
  summary: string;
};

export function cleanString(value: unknown): string {
  return String(value ?? "").trim();
}

export function fieldPrompts(question: OperatorQuestion): FieldPrompt[] {
  const fields = question.expected_answer_schema?.fields;
  return Array.isArray(fields) ? fields.filter((field): field is FieldPrompt => Boolean(field && typeof field === "object")) : [];
}

export function schemaString(question: OperatorQuestion, key: string): string {
  return cleanString((question.expected_answer_schema || {})[key]);
}

function rawFieldTitle(field: FieldPrompt): string {
  return cleanString(field.display_title || field.observed_label || field.label);
}

export function fieldLabel(field: FieldPrompt): string {
  return rawFieldTitle(field) || cleanString(field.field_key) || "Application field";
}

export function fieldOptions(field: FieldPrompt): string[] {
  return Array.isArray(field.options) ? field.options.map((option) => String(option).trim()).filter(Boolean) : [];
}

export function isChoiceField(field: FieldPrompt): boolean {
  const kind = cleanString(field.question_kind || field.field_kind || field.control_kind).toLowerCase();
  return isMultipleChoiceField(field) || field.is_choice === true || kind === "choice" || kind.includes("radio") || kind.includes("select") || kind.includes("combobox");
}

export function taskIds(field: FieldPrompt): number[] | undefined {
  const id = Number(field.id);
  return Number.isFinite(id) && id > 0 ? [id] : undefined;
}

export function answerModes(field: FieldPrompt): string[] {
  return Array.isArray(field.answer_modes) ? field.answer_modes.map((mode) => String(mode).trim()).filter(Boolean) : [];
}

export function isDocumentField(field: FieldPrompt): boolean {
  const modes = answerModes(field);
  return modes.includes("upload_file") || modes.includes("generate_artifact") || modes.includes("choose_artifact");
}

function documentCapability(field: FieldPrompt): Record<string, unknown> | null {
  return field.document_capability && typeof field.document_capability === "object" ? (field.document_capability as Record<string, unknown>) : null;
}

export function documentKind(field: FieldPrompt): string {
  return cleanString(documentCapability(field)?.document_kind || "application_document");
}

export function humanKind(value: string): string {
  return value.replace(/[_-]+/g, " ").trim().replace(/^application document$/, "document");
}

/** Accepted extensions without dots, e.g. ["pdf", "docx"]. */
export function acceptedExtensions(field: FieldPrompt): string[] {
  const capability = documentCapability(field);
  const raw = Array.isArray(field.accepted_file_extensions) && field.accepted_file_extensions.length ? field.accepted_file_extensions : capability?.accepted_file_extensions;
  const list = Array.isArray(raw) ? raw.map((item) => String(item).trim().replace(/^\./, "").toLowerCase()).filter(Boolean) : [];
  return list.length ? list : ["pdf", "doc", "docx", "txt", "rtf"];
}

export function artifactOptions(field: FieldPrompt): ArtifactOption[] {
  const raw = Array.isArray(field.artifact_options) ? field.artifact_options : [];
  return raw.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const artifact = item as Record<string, unknown>;
    const id = Number(artifact.id);
    if (!Number.isFinite(id) || id <= 0) return [];
    const metadata = artifact.metadata_json && typeof artifact.metadata_json === "object" ? (artifact.metadata_json as Record<string, unknown>) : {};
    const name =
      cleanString(artifact.display_name) ||
      cleanString(metadata.display_name) ||
      cleanString(metadata.display_label) ||
      cleanString(metadata.name) ||
      cleanString(artifact.filename) ||
      `Document ${id}`;
    return [{ id, name, preview: cleanString(artifact.text_preview) }];
  });
}

function requirednessState(field: FieldPrompt): string {
  const req = field.requiredness && typeof field.requiredness === "object" ? (field.requiredness as Record<string, unknown>) : {};
  return cleanString(req.state).toLowerCase();
}

export function fieldRequired(field: FieldPrompt): boolean {
  if ("required" in field) return field.required !== false;
  const state = requirednessState(field);
  return state.startsWith("required") || state === "unknown_blocking";
}

export function requiredBadge(field: FieldPrompt): "required" | "optional" | "voluntary" {
  const state = requirednessState(field);
  if (state.includes("voluntary")) return "voluntary";
  if (state.startsWith("optional") || field.required === false) return "optional";
  return fieldRequired(field) ? "required" : "optional";
}

export function storedContext(field: FieldPrompt): string {
  const preview = field.interpreted_answer_preview;
  if (preview && typeof preview === "object" && "resolved_value" in preview) {
    const value = cleanString((preview as { resolved_value?: unknown }).resolved_value);
    if (value) return `Saved answer that may fit: ${value}`;
  }
  const stored = cleanString(field.stored_answer);
  return stored ? `Saved answer: ${stored}` : "";
}

export function finalActionContract(question: OperatorQuestion): FinalActionContract | null {
  if (question.question_type !== "final_action_approval") return null;
  const value = question.expected_answer_schema?.final_action;
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const contract = value as Record<string, unknown>;
  const approvalToken = cleanString(contract.approval_token);
  const choices = Array.isArray(contract.choices)
    ? contract.choices.flatMap((choice) => {
        if (!choice || typeof choice !== "object" || Array.isArray(choice)) return [];
        const item = choice as Record<string, unknown>;
        const label = cleanString(item.label);
        const decision = cleanString(item.decision);
        return label && (decision === "approve" || decision === "decline") ? [{ label, decision: decision as "approve" | "decline" }] : [];
      })
    : [];
  return approvalToken && choices.length
    ? {
        approvalToken,
        choices,
        submitLabel: cleanString(contract.submit_label),
        paymentFloor: contract.payment_floor === true,
        summary: cleanString(contract.summary)
      }
    : null;
}

export function schemaOptions(question: OperatorQuestion): string[] {
  const options = (question.expected_answer_schema || {}).options;
  return Array.isArray(options) ? options.map((option) => String(option).trim()).filter(Boolean) : [];
}

export function deferFor(required: boolean, lastOption?: string): { label: string; hint: string } {
  return required
    ? { label: "Leave this one for me", hint: "Lumen won't fill it, and this required field stays open for you. You can also reply with an instruction below." }
    : { label: "Skip it", hint: `Or reply with an instruction below, like “use ${lastOption || "Other"} and remember it”.` };
}
