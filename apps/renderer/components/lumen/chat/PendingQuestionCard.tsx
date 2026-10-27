"use client";

import { useId, useRef, useState, type ReactNode } from "react";
import { ArrowRight, Bookmark, Check, ChevronDown, CreditCard, FileText, FileUp, Info, MessageSquareReply, PenLine, ShieldCheck, Upload } from "lucide-react";
import type { ApplicationArtifact, OperatorQuestion } from "@/lib/api";
import {
  confirmationChoiceContractReady,
  confirmationCoverageCaveat,
  confirmationEvidenceExplanation,
  confirmationIsRequired,
  isMultipleChoiceField,
  multiSelectAnswer,
  proposedConfirmationValue,
  proposedMultiSelection,
  shouldRenderFieldSummaryList
} from "@/lib/pendingQuestionConfirmation";
import { LumenMark } from "@/components/lumen/brand/LumenMark";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { fileMatchesAccept } from "@/lib/format";
import { cn } from "@/lib/utils";

type ArtifactOption = {
  id?: unknown;
  display_name?: unknown;
  filename?: unknown;
  text_preview?: unknown;
  metadata_json?: unknown;
};

type DocumentCapability = {
  document_kind?: unknown;
  accepted_file_extensions?: unknown;
};

type FieldPrompt = {
  id?: unknown;
  label?: unknown;
  observed_label?: unknown;
  field_key?: unknown;
  display_title?: unknown;
  display_subtitle?: unknown;
  display_reason?: unknown;
  field_kind?: unknown;
  control_kind?: unknown;
  question_kind?: unknown;
  ask_reason?: unknown;
  question_contract_valid?: unknown;
  question_contract_error?: unknown;
  options?: unknown;
  is_choice?: unknown;
  // true: a checkbox group, the user picks one or more of `options`.
  multiple?: boolean;
  required?: unknown;
  requiredness?: unknown;
  requires_fresh_observation?: unknown;
  needs_option_observation?: unknown;
  choice_question_state?: unknown;
  options_binding_status?: unknown;
  interpreted_answer_preview?: unknown;
  stored_answer?: unknown;
  proposed_answer?: unknown;
  proposed_option?: unknown;
  evidence_coverage?: unknown;
  source_coverage?: unknown;
  source_coverage_explanation?: unknown;
  coverage_explanation?: unknown;
  confirmation_required?: unknown;
  answer_modes?: unknown;
  document_capability?: unknown;
  artifact_options?: unknown;
  accepted_file_extensions?: unknown;
};

// onSelect carries an optional structured payload (e.g. {keep_proposed, task_ids}) that rides the
// chat message through to the backend's answer path so "Approve" applies the proposed value verbatim.
export type SelectFn = (text: string, extra?: Record<string, unknown>) => void;
export type FinalActionFn = (questionId: number, approvalToken: string, decision: "approve" | "decline", label: string) => void;
export type UploadArtifactFn = (questionId: number, file: File) => Promise<void>;
export type GenerateArtifactFn = (questionId: number, instructions?: string, artifactKind?: string) => Promise<ApplicationArtifact | null>;
export type UpdateArtifactFn = (artifactId: number, textContent: string) => Promise<ApplicationArtifact>;
export type ApplyArtifactFn = (questionId: number, artifactId: number) => Promise<void>;

function fieldPrompts(question: OperatorQuestion): FieldPrompt[] {
  const fields = question.expected_answer_schema?.fields;
  return Array.isArray(fields) ? fields.filter((field): field is FieldPrompt => Boolean(field && typeof field === "object")) : [];
}

function cleanString(value: unknown): string {
  return String(value || "").trim();
}

function schemaString(question: OperatorQuestion, key: string): string {
  const schema = question.expected_answer_schema || {};
  return cleanString(schema[key]);
}

function rawFieldTitle(field: FieldPrompt): string {
  return cleanString(field.display_title || field.observed_label || field.label);
}

function fieldLabel(field: FieldPrompt): string {
  return rawFieldTitle(field) || cleanString(field.field_key) || "Application field";
}

function fieldOptions(field: FieldPrompt): string[] {
  return Array.isArray(field.options) ? field.options.map((option) => String(option).trim()).filter(Boolean) : [];
}

function isChoiceField(field: FieldPrompt): boolean {
  const kind = cleanString(field.question_kind || field.field_kind || field.control_kind).toLowerCase();
  return isMultipleChoiceField(field) || field.is_choice === true || kind === "choice" || kind.includes("radio") || kind.includes("select") || kind.includes("combobox");
}

function taskIds(field: FieldPrompt): number[] | undefined {
  const id = Number(field.id);
  return Number.isFinite(id) && id > 0 ? [id] : undefined;
}

function answerModes(field: FieldPrompt): string[] {
  return Array.isArray(field.answer_modes) ? field.answer_modes.map((mode) => String(mode).trim()).filter(Boolean) : [];
}

function documentCapability(field: FieldPrompt): DocumentCapability | null {
  return field.document_capability && typeof field.document_capability === "object" ? (field.document_capability as DocumentCapability) : null;
}

function artifactOptions(field: FieldPrompt): ArtifactOption[] {
  return Array.isArray(field.artifact_options) ? field.artifact_options.filter((item): item is ArtifactOption => Boolean(item && typeof item === "object")) : [];
}

function artifactOptionName(artifact: ArtifactOption, id: number): string {
  if (typeof artifact.display_name === "string" && artifact.display_name.trim()) return artifact.display_name.trim();
  const metadata = artifact.metadata_json && typeof artifact.metadata_json === "object" ? (artifact.metadata_json as Record<string, unknown>) : {};
  for (const key of ["display_name", "display_label", "name"]) {
    const value = metadata[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return String(artifact.filename || `Document ${id}`);
}

function acceptedExtensions(field: FieldPrompt): string {
  const capability = documentCapability(field);
  const raw = Array.isArray(field.accepted_file_extensions) && field.accepted_file_extensions.length ? field.accepted_file_extensions : capability?.accepted_file_extensions;
  const list = Array.isArray(raw) ? raw.map((item) => String(item).trim().replace(/^\./, "")).filter(Boolean) : [];
  return list.length ? list.map((item) => `.${item}`).join(",") : ".pdf,.doc,.docx,.txt,.rtf";
}

function documentKind(field: FieldPrompt): string {
  return String(documentCapability(field)?.document_kind || "application_document").trim();
}

function humanKind(value: string): string {
  return value.replace(/[_-]+/g, " ").trim();
}

function isDocumentField(field: FieldPrompt): boolean {
  const modes = answerModes(field);
  return modes.includes("upload_file") || modes.includes("generate_artifact") || modes.includes("choose_artifact");
}

function requirednessState(field: FieldPrompt): string {
  const req = field.requiredness && typeof field.requiredness === "object" ? (field.requiredness as Record<string, unknown>) : {};
  return cleanString(req.state).toLowerCase();
}

function fieldRequired(field: FieldPrompt): boolean {
  if ("required" in field) return field.required !== false;
  const state = requirednessState(field);
  return state.startsWith("required") || state === "unknown_blocking";
}

function requiredBadge(field: FieldPrompt): "required" | "optional" | "voluntary" {
  const state = requirednessState(field);
  if (state.includes("voluntary")) return "voluntary";
  if (state.startsWith("optional") || field.required === false) return "optional";
  return fieldRequired(field) ? "required" : "optional";
}

function storedContext(field: FieldPrompt): string {
  const preview = field.interpreted_answer_preview;
  if (preview && typeof preview === "object" && "resolved_value" in preview) {
    const value = String((preview as { resolved_value?: unknown }).resolved_value || "").trim();
    if (value) return `Saved answer that may fit: ${value}`;
  }
  const stored = String(field.stored_answer || "").trim();
  return stored ? `Saved answer: ${stored}` : "";
}

function finalActionContract(question: OperatorQuestion): {
  approvalToken: string;
  choices: Array<{ label: string; decision: "approve" | "decline" }>;
  submitLabel: string;
  paymentFloor: boolean;
  summary: string;
} | null {
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

function schemaOptions(question: OperatorQuestion): string[] {
  const schema = question.expected_answer_schema || {};
  const options = Array.isArray(schema.options) ? schema.options : [];
  return options.map((option) => String(option).trim()).filter(Boolean);
}

// ---------------------------------------------------------------------------------------------------
// Building blocks

function CardShell({
  eyebrow,
  icon,
  badge,
  tone = "attention",
  children
}: {
  eyebrow: string;
  icon: ReactNode;
  badge?: ReactNode;
  tone?: "attention" | "brand";
  children: ReactNode;
}) {
  return (
    <section
      aria-label={eyebrow}
      className={cn(
        "animate-enter-up overflow-hidden rounded-2xl border bg-surface-1 shadow-md",
        tone === "attention" ? "border-attention/30" : "border-brand/30"
      )}
    >
      <header
        className={cn(
          "flex items-center gap-2 border-b px-4 py-2 [&_svg]:size-4",
          tone === "attention" ? "border-attention/15 bg-attention/[0.07] text-attention-ink" : "border-brand/15 bg-brand/[0.06] text-brand-ink"
        )}
      >
        {icon}
        <span className="text-13 font-semibold">{eyebrow}</span>
        {badge ? <span className="ml-auto flex items-center gap-1.5">{badge}</span> : null}
      </header>
      <div className="px-5 pb-5 pt-4">{children}</div>
    </section>
  );
}

function RequirementBadge({ field }: { field: FieldPrompt }) {
  const kind = requiredBadge(field);
  return <Badge tone={kind === "required" ? "attention" : "neutral"}>{kind === "required" ? "Required" : kind === "voluntary" ? "Voluntary" : "Optional"}</Badge>;
}

function QuestionHeading({ question, field, hideReason }: { question: OperatorQuestion; field: FieldPrompt | null; hideReason?: boolean }) {
  const title = (field && (schemaString(question, "display_title") || rawFieldTitle(field))) || question.prompt;
  const subtitle = field ? schemaString(question, "display_subtitle") || cleanString(field.display_subtitle) : "";
  const reason = field ? schemaString(question, "display_reason") || cleanString(field.display_reason || field.ask_reason) : "";
  const context = title !== question.prompt ? cleanString(question.prompt) : "";
  return (
    <div>
      <h3 className="text-base font-semibold leading-snug text-fg">{title || "Lumen has a question"}</h3>
      {subtitle ? (
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          <Badge tone="outline">{subtitle}</Badge>
        </div>
      ) : null}
      {context ? <p className="mt-2 whitespace-pre-line text-sm text-fg-muted">{context}</p> : null}
      {!hideReason && reason && reason !== context ? (
        <p className="mt-2 flex gap-2 text-13 text-fg-muted">
          <Info className="mt-0.5 size-3.5 shrink-0 text-fg-subtle" />
          <span>{reason}</span>
        </p>
      ) : null}
    </div>
  );
}

function OptionList({
  options,
  label,
  disabled,
  onPick,
  defer
}: {
  options: string[];
  label: string;
  disabled: boolean;
  onPick: (option: string) => void;
  defer?: { label: string; hint: string; onPick: () => void };
}) {
  return (
    <div className="mt-4">
      <div role="group" aria-label={label} className="overflow-hidden rounded-xl border border-line">
        {options.map((option, index) => (
          <button
            key={`${option}-${index}`}
            type="button"
            disabled={disabled}
            onClick={() => onPick(option)}
            className="group/option flex w-full items-center gap-3 border-b border-line px-3.5 py-2.5 text-left text-sm text-fg transition-colors last:border-b-0 hover:bg-brand/[0.06] focus-ring-inset disabled:opacity-50"
          >
            <span className="flex size-4 shrink-0 items-center justify-center rounded-full border border-line-strong transition group-hover/option:border-brand">
              <span className="size-2 scale-0 rounded-full bg-brand transition group-hover/option:scale-100" />
            </span>
            <span className="min-w-0 flex-1">{option}</span>
            <ArrowRight className="size-3.5 shrink-0 text-brand-ink opacity-0 transition group-hover/option:opacity-100" />
          </button>
        ))}
        {defer ? (
          <button
            type="button"
            disabled={disabled}
            onClick={defer.onPick}
            className="flex w-full items-center gap-3 bg-fg/[0.025] px-3.5 py-2.5 text-left text-sm text-fg-muted transition-colors hover:bg-fg/[0.05] hover:text-fg focus-ring-inset disabled:opacity-50"
          >
            <MessageSquareReply className="size-4 shrink-0" />
            <span>{defer.label}</span>
          </button>
        ) : null}
      </div>
      {defer ? <p className="mt-2 text-xs text-fg-subtle">{defer.hint}</p> : null}
    </div>
  );
}

type MultiSelectAnswer = { content: string; selected_options: string[] };

// A checkbox group: rows toggle, nothing is sent until the user presses Continue.
function MultiOptionList({
  options,
  label,
  disabled,
  initial,
  onSubmit,
  defer
}: {
  options: string[];
  label: string;
  disabled: boolean;
  initial?: string[];
  onSubmit: (answer: MultiSelectAnswer) => void;
  defer?: { label: string; hint: string; onPick: () => void };
}) {
  const [selected, setSelected] = useState<string[]>(() => (initial || []).filter((option) => options.includes(option)));
  const toggle = (option: string) => setSelected((prev) => (prev.includes(option) ? prev.filter((item) => item !== option) : [...prev, option]));
  const answer = multiSelectAnswer(options, selected);
  const count = answer?.selected_options.length ?? 0;
  return (
    <div className="mt-4">
      <div role="group" aria-label={label} className="overflow-hidden rounded-xl border border-line">
        {options.map((option, index) => {
          const checked = selected.includes(option);
          return (
            <button
              key={`${option}-${index}`}
              type="button"
              role="checkbox"
              aria-checked={checked}
              disabled={disabled}
              onClick={() => toggle(option)}
              className={cn(
                "group/option flex w-full items-center gap-3 border-b border-line px-3.5 py-2.5 text-left text-sm text-fg transition-colors last:border-b-0 hover:bg-brand/[0.06] focus-ring-inset disabled:opacity-50",
                checked && "bg-brand/[0.05]"
              )}
            >
              <span
                aria-hidden
                className={cn(
                  "flex size-4 shrink-0 items-center justify-center rounded-[5px] border transition",
                  checked ? "border-brand bg-brand text-brand-fg" : "border-line-strong group-hover/option:border-brand"
                )}
              >
                {checked ? <Check className="size-3" strokeWidth={3} /> : null}
              </span>
              <span className="min-w-0 flex-1">{option}</span>
            </button>
          );
        })}
        {defer ? (
          <button
            type="button"
            disabled={disabled}
            onClick={defer.onPick}
            className="flex w-full items-center gap-3 bg-fg/[0.025] px-3.5 py-2.5 text-left text-sm text-fg-muted transition-colors hover:bg-fg/[0.05] hover:text-fg focus-ring-inset disabled:opacity-50"
          >
            <MessageSquareReply className="size-4 shrink-0" />
            <span>{defer.label}</span>
          </button>
        ) : null}
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button variant="primary" size="sm" disabled={disabled || !answer} onClick={() => answer && onSubmit(answer)}>
          <Check /> {count ? `Continue with ${count}` : "Continue"}
        </Button>
        <span className="text-xs text-fg-subtle">{count ? "Nothing is sent until you continue." : "Pick one or more."}</span>
      </div>
      {defer ? <p className="mt-2 text-xs text-fg-subtle">{defer.hint}</p> : null}
    </div>
  );
}

function deferFor(required: boolean, lastOption?: string) {
  return required
    ? { label: "Leave this one for me", hint: "Lumen won't fill it, and this required field stays open for you. You can also reply with an instruction below." }
    : { label: "Skip it", hint: `Or reply with an instruction below, like “use ${lastOption || "Other"} and remember it”.` };
}

function EvidenceNote({ field }: { field: FieldPrompt }) {
  const explanation = confirmationEvidenceExplanation(field);
  const caveat = confirmationCoverageCaveat(field);
  const [open, setOpen] = useState(false);
  if (!explanation && !caveat) return null;
  return (
    <div className="mt-3 text-13 text-fg-muted">
      {explanation ? (
        <p className="flex gap-2">
          <Info className="mt-0.5 size-3.5 shrink-0 text-fg-subtle" />
          <span>{explanation}</span>
        </p>
      ) : null}
      {caveat ? (
        <>
          <button
            type="button"
            onClick={() => setOpen((value) => !value)}
            aria-expanded={open}
            className="ml-5 mt-1 inline-flex items-center gap-1 rounded text-xs font-medium text-fg-subtle transition hover:text-fg focus-ring"
          >
            How Lumen checked
            <ChevronDown className={cn("size-3 transition-transform", open && "rotate-180")} />
          </button>
          {open ? <p className="ml-5 mt-1 animate-enter-fade text-xs leading-relaxed text-fg-subtle">{caveat}</p> : null}
        </>
      ) : null}
    </div>
  );
}

function Suggestion({ value, approvalRequired }: { value: string; approvalRequired: boolean }) {
  return (
    <div className="mt-4 rounded-xl border border-brand/25 bg-brand/[0.05] px-4 py-3">
      <div className="flex items-center gap-2">
        <LumenMark className="size-3.5" />
        <span className="text-xs font-semibold text-brand-ink">Lumen suggests</span>
        <span className="ml-auto text-2xs text-fg-subtle">{approvalRequired ? "Not on the page until you approve" : "Not on the page yet"}</span>
      </div>
      <p className="mt-1.5 whitespace-pre-wrap text-15 leading-relaxed text-fg [overflow-wrap:anywhere]">{value}</p>
    </div>
  );
}

type ProposalEditMode = "idle" | "edit" | "reject";

// Confirm-ask: the proposal stays pending until the user explicitly approves or supplies an answer.
function ProposedAnswer({ field, sending, onSelect }: { field: FieldPrompt; sending: boolean; onSelect: SelectFn }) {
  const proposed = proposedConfirmationValue(field);
  const options = fieldOptions(field);
  const ids = taskIds(field);
  const [editMode, setEditMode] = useState<ProposalEditMode>("idle");
  const [draft, setDraft] = useState("");
  const approvalRequired = confirmationIsRequired(field);
  const required = fieldRequired(field);
  const keep = () => onSelect(proposed, { keep_proposed: true, task_ids: ids });
  const resetEditor = () => {
    setDraft("");
    setEditMode("idle");
  };

  // A choice field: the proposed pick must match a real option to be approvable. A multi-select
  // proposal ("A, B") must name only real options.
  const multiple = isMultipleChoiceField(field);
  const proposedSelection = multiple ? proposedMultiSelection(options, proposed) : [];
  const matchedOption = multiple
    ? proposedSelection.join(", ") || undefined
    : options.find((option) => option.toLowerCase() === proposed.toLowerCase());

  if (options.length) {
    const defer = deferFor(required, options[options.length - 1]);
    return (
      <>
        <Suggestion value={matchedOption || proposed} approvalRequired={approvalRequired} />
        <EvidenceNote field={field} />
        {matchedOption && editMode === "idle" ? (
          <div className="mt-4 flex flex-wrap gap-2">
            <Button variant="primary" size="sm" disabled={sending} onClick={keep}>
              <Check /> {proposedSelection.length > 1 ? `Use these ${proposedSelection.length}` : `Use “${matchedOption}”`}
            </Button>
            <Button variant="ghost" size="sm" disabled={sending} onClick={() => setEditMode("edit")}>
              {multiple ? "Change the picks" : "Choose another"}
            </Button>
          </div>
        ) : null}
        {!matchedOption ? (
          <p className="mt-3 text-13 text-attention-ink">
            {multiple ? "That suggestion doesn't match the form's options, so pick them yourself." : "That suggestion isn't one of the form's options, so pick one yourself."}
          </p>
        ) : null}
        {editMode !== "idle" || !matchedOption ? (
          <div className="animate-enter-fade">
            {multiple ? (
              <MultiOptionList
                options={options}
                label={fieldLabel(field)}
                disabled={sending}
                initial={proposedSelection}
                onSubmit={(answer) => onSelect(answer.content, { selected_options: answer.selected_options, task_ids: ids })}
                defer={{ ...defer, onPick: () => onSelect("Skip this field", { skip: true, task_ids: ids }) }}
              />
            ) : (
              <OptionList
                options={options}
                label={fieldLabel(field)}
                disabled={sending}
                onPick={(option) => onSelect(option, { selected_option: option, task_ids: ids })}
                defer={{ ...defer, onPick: () => onSelect("Skip this field", { skip: true, task_ids: ids }) }}
              />
            )}
            {matchedOption ? (
              <Button variant="ghost" size="xs" className="mt-2" disabled={sending} onClick={resetEditor}>
                Back to the suggestion
              </Button>
            ) : null}
          </div>
        ) : null}
      </>
    );
  }

  // Free text: approve, edit, and write-your-own are distinct. Edit and write only open a local
  // draft; neither sends an answer until the user submits the replacement.
  return (
    <>
      {editMode === "idle" ? <Suggestion value={proposed} approvalRequired={approvalRequired} /> : null}
      <EvidenceNote field={field} />
      {editMode !== "idle" ? (
        <div className="mt-4 animate-enter-fade space-y-2.5">
          <Textarea
            autoFocus
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            rows={editMode === "edit" ? 5 : 3}
            aria-label={editMode === "edit" ? "Edit the suggested answer" : "Your answer"}
            placeholder={editMode === "reject" ? "Write the answer Lumen should use…" : undefined}
          />
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="primary" size="sm" disabled={sending || !draft.trim()} onClick={() => onSelect(draft.trim(), { task_ids: ids })}>
              <Check /> Use this answer
            </Button>
            <Button variant="ghost" size="sm" disabled={sending} onClick={resetEditor}>
              Cancel
            </Button>
            <span className="text-xs text-fg-subtle">Nothing is sent until you choose Use this answer.</span>
          </div>
        </div>
      ) : (
        <div className="mt-4 flex flex-wrap gap-2">
          <Button variant="primary" size="sm" disabled={sending} onClick={keep}>
            <Check /> Use this answer
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={sending}
            onClick={() => onSelect(proposed, { keep_proposed: true, task_ids: ids, save_scope: "permanent" })}
            title="Use it now and remember it in your profile"
          >
            <Bookmark /> Use & remember
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={sending}
            onClick={() => {
              setDraft(proposed);
              setEditMode("edit");
            }}
          >
            <PenLine /> Edit
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={sending}
            onClick={() => {
              setDraft("");
              setEditMode("reject");
            }}
          >
            Write my own
          </Button>
        </div>
      )}
    </>
  );
}

type DocumentTab = "saved" | "upload" | "generate";

function DocumentArtifactControls({
  question,
  field,
  sending,
  onUploadArtifact,
  onGenerateArtifact,
  onUpdateArtifact,
  onApplyArtifact
}: {
  question: OperatorQuestion;
  field: FieldPrompt;
  sending: boolean;
  onUploadArtifact: UploadArtifactFn;
  onGenerateArtifact: GenerateArtifactFn;
  onUpdateArtifact: UpdateArtifactFn;
  onApplyArtifact: ApplyArtifactFn;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const inputId = useId();
  const modes = answerModes(field);
  const options = artifactOptions(field).filter((artifact) => Number.isFinite(Number(artifact.id)) && Number(artifact.id) > 0);
  const canUpload = modes.includes("upload_file");
  const canGenerate = modes.includes("generate_artifact");
  const canChoose = modes.includes("choose_artifact") && options.length > 0;
  const tabs: Array<{ key: DocumentTab; label: string; icon: ReactNode }> = [
    ...(canChoose ? [{ key: "saved" as const, label: `Saved (${options.length})`, icon: <FileText /> }] : []),
    ...(canUpload ? [{ key: "upload" as const, label: "Upload", icon: <Upload /> }] : []),
    ...(canGenerate ? [{ key: "generate" as const, label: "Write with Lumen", icon: <PenLine /> }] : [])
  ];
  const [tab, setTab] = useState<DocumentTab>(tabs[0]?.key ?? "upload");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [draftArtifact, setDraftArtifact] = useState<ApplicationArtifact | null>(null);
  const [draftText, setDraftText] = useState("");
  const [instructions, setInstructions] = useState("");
  const [dragging, setDragging] = useState(false);
  const kind = humanKind(documentKind(field)).replace(/^application document$/, "document");

  const run = async (operation: () => Promise<void>, success?: string) => {
    setBusy(true);
    setStatus(null);
    try {
      await operation();
      if (success) setStatus({ tone: "ok", text: success });
    } catch (err) {
      setStatus({ tone: "error", text: err instanceof Error ? err.message : "That didn't work. Try again." });
    } finally {
      setBusy(false);
    }
  };

  const accepted = acceptedExtensions(field);
  const upload = (file: File) => {
    if (!fileMatchesAccept(file, accepted)) {
      setStatus({ tone: "error", text: `This field takes ${accepted.split(",").join(", ")} files. ${file.name} isn't one.` });
      return;
    }
    void run(async () => {
      await onUploadArtifact(question.id, file);
    }, `Uploaded ${file.name}. Lumen checks the live field when it resumes.`);
  };

  const generate = () =>
    run(async () => {
      const artifact = await onGenerateArtifact(question.id, instructions.trim() || undefined, documentKind(field));
      if (artifact) {
        setDraftArtifact(artifact);
        setDraftText(artifact.text_content || artifact.text_preview || "");
      }
    });

  const applyDraft = () =>
    run(async () => {
      if (!draftArtifact) return;
      let artifact = draftArtifact;
      if (draftText.trim() && draftText !== (draftArtifact.text_content || "")) {
        artifact = await onUpdateArtifact(draftArtifact.id, draftText.trim());
        setDraftArtifact(artifact);
      }
      await onApplyArtifact(question.id, artifact.id);
    }, "Saved for this form. Lumen checks the live field when it resumes.");

  const applyExisting = (artifactId: number) =>
    run(async () => {
      await onApplyArtifact(question.id, artifactId);
    }, "Saved for this form. Lumen checks the live field when it resumes.");

  const disabled = sending || busy;

  return (
    <div className="mt-4">
      {tabs.length > 1 ? (
        <div role="tablist" aria-label="How to provide the document" className="mb-3 inline-flex rounded-lg border border-line bg-fg/[0.04] p-0.5">
          {tabs.map((item) => (
            <button
              key={item.key}
              type="button"
              role="tab"
              aria-selected={tab === item.key}
              onClick={() => setTab(item.key)}
              className={cn(
                "inline-flex h-7 items-center gap-1.5 rounded-md px-3 text-13 font-medium transition focus-ring-inset [&_svg]:size-3.5",
                tab === item.key ? "bg-surface-1 text-fg shadow-sm" : "text-fg-muted hover:text-fg"
              )}
            >
              {item.icon}
              {item.label}
            </button>
          ))}
        </div>
      ) : null}

      {tab === "saved" && canChoose ? (
        <div className="space-y-2">
          {options.map((artifact) => {
            const id = Number(artifact.id);
            const name = artifactOptionName(artifact, id);
            const preview = String(artifact.text_preview || "").trim();
            return (
              <button
                key={id}
                type="button"
                disabled={disabled}
                onClick={() => applyExisting(id)}
                className="group/doc flex w-full items-start gap-3 rounded-xl border border-line bg-surface-1 p-3 text-left transition hover:border-brand/40 hover:bg-brand/[0.04] focus-ring disabled:opacity-50"
              >
                <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-fg/[0.06] text-fg-muted">
                  <FileText className="size-4" />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium text-fg">{name}</span>
                  {preview ? <span className="mt-0.5 block line-clamp-2 text-xs text-fg-muted">{preview}</span> : null}
                </span>
                <span className="mt-1.5 shrink-0 text-xs font-medium text-brand-ink opacity-0 transition group-hover/doc:opacity-100">Use this</span>
              </button>
            );
          })}
        </div>
      ) : null}

      {tab === "upload" && canUpload ? (
        <>
          <input
            id={inputId}
            ref={fileRef}
            type="file"
            accept={accepted}
            className="sr-only"
            tabIndex={-1}
            onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = "";
              if (file) upload(file);
            }}
          />
          <button
            type="button"
            disabled={disabled}
            onClick={() => fileRef.current?.click()}
            onDragOver={(event) => {
              event.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(event) => {
              event.preventDefault();
              setDragging(false);
              const file = event.dataTransfer.files?.[0];
              if (file && !disabled) upload(file);
            }}
            className={cn(
              "flex w-full flex-col items-center justify-center gap-1.5 rounded-xl border border-dashed px-4 py-6 text-center transition focus-ring disabled:opacity-50",
              dragging ? "border-brand bg-brand/[0.06]" : "border-line-strong hover:border-fg-subtle hover:bg-fg/[0.02]"
            )}
          >
            {busy ? <Spinner className="size-5 text-fg-muted" /> : <FileUp className="size-5 text-fg-muted" />}
            <span className="text-sm font-medium text-fg">{busy ? "Uploading…" : `Drop a ${kind} here, or choose a file`}</span>
            <span className="text-xs text-fg-subtle">{accepted.replaceAll(",", " ")}</span>
          </button>
        </>
      ) : null}

      {tab === "generate" && canGenerate ? (
        draftArtifact ? (
          <div className="space-y-2.5">
            <p className="text-xs font-medium text-fg-muted">Draft — edit anything before you use it</p>
            <Textarea value={draftText} onChange={(event) => setDraftText(event.target.value)} rows={9} aria-label="Draft document" />
            <div className="flex flex-wrap gap-2">
              <Button variant="primary" size="sm" disabled={disabled || !draftText.trim()} loading={busy} onClick={applyDraft}>
                {!busy ? <Check /> : null} Use this draft
              </Button>
              <Button variant="ghost" size="sm" disabled={disabled} onClick={generate}>
                Write another
              </Button>
            </div>
          </div>
        ) : (
          <div className="space-y-2.5">
            <Textarea
              value={instructions}
              onChange={(event) => setInstructions(event.target.value)}
              rows={2}
              aria-label="Notes for the draft"
              placeholder="Anything to emphasize? (optional)"
              className="min-h-0"
            />
            <Button variant="primary" size="sm" disabled={disabled} loading={busy} onClick={generate}>
              {!busy ? <PenLine /> : null} {busy ? "Writing…" : `Write a ${kind}`}
            </Button>
            <p className="text-xs text-fg-subtle">Lumen drafts from your profile and résumé. You review it before anything reaches the form.</p>
          </div>
        )
      ) : null}

      {status ? (
        <p role={status.tone === "error" ? "alert" : "status"} className={cn("mt-3 text-13", status.tone === "error" ? "text-danger-ink" : "text-brand-ink")}>
          {status.text}
        </p>
      ) : null}
    </div>
  );
}

function FinalActionApproval({
  question,
  contract,
  sending,
  onFinalAction
}: {
  question: OperatorQuestion;
  contract: ReturnType<typeof finalActionContract>;
  sending: boolean;
  onFinalAction: FinalActionFn;
}) {
  if (!contract) {
    return (
      <CardShell eyebrow="Approve the final step" icon={<ShieldCheck />}>
        <p className="whitespace-pre-line text-sm text-fg-muted">{question.prompt}</p>
        <p className="mt-3 rounded-lg border border-danger/25 bg-danger/[0.06] px-3 py-2 text-13 text-danger-ink">
          This approval is out of date or incomplete, so Lumen won&rsquo;t take the final step. It will ask again if it can.
        </p>
      </CardShell>
    );
  }
  return (
    <CardShell
      eyebrow={contract.paymentFloor ? "Confirm the payment" : "Approve the final step"}
      icon={contract.paymentFloor ? <CreditCard /> : <ShieldCheck />}
      badge={contract.paymentFloor ? <Badge tone="attention">Involves payment</Badge> : null}
    >
      {contract.submitLabel ? (
        <div className="mb-3">
          <p className="eyebrow">Lumen will click</p>
          <p className="mt-1 text-lg font-semibold text-fg">“{contract.submitLabel}”</p>
        </div>
      ) : null}
      {contract.summary ? (
        <div className="mb-3 flex items-start gap-3 rounded-xl border border-attention/25 bg-attention/[0.06] px-4 py-3">
          <CreditCard className="mt-0.5 size-4 shrink-0 text-attention-ink" />
          <p className="text-sm font-medium text-fg">{contract.summary}</p>
        </div>
      ) : null}
      <p className="whitespace-pre-line text-sm leading-relaxed text-fg-muted">{question.prompt}</p>
      <div className="mt-4 flex flex-wrap gap-2">
        {contract.choices.map((choice) => (
          <Button
            key={`${choice.decision}-${choice.label}`}
            variant={choice.decision === "approve" ? (contract.paymentFloor ? "attention" : "primary") : "secondary"}
            size="md"
            disabled={sending}
            onClick={() => onFinalAction(question.id, contract.approvalToken, choice.decision, choice.label)}
          >
            {choice.decision === "approve" ? <Check /> : null}
            {choice.label}
          </Button>
        ))}
      </div>
      <p className="mt-3 text-xs text-fg-subtle">This approval works once, for that exact button. If the page changes first, Lumen asks again.</p>
    </CardShell>
  );
}

function InvalidQuestionContract({ field }: { field: FieldPrompt }) {
  const error = cleanString(field.question_contract_error) || "The field label or option snapshot is missing.";
  const refreshing = Boolean(
    field.requires_fresh_observation ||
      field.needs_option_observation ||
      cleanString(field.choice_question_state) === "reobserve_or_hydrate_required" ||
      cleanString(field.options_binding_status) === "mismatched_reobserve_required" ||
      !fieldOptions(field).length
  );
  return refreshing ? (
    <p className="mt-4 flex items-center gap-2.5 text-13 text-fg-muted">
      <Spinner className="size-3.5" />
      Re-reading the field&rsquo;s options from the page before asking.
    </p>
  ) : (
    <p className="mt-4 rounded-lg border border-danger/25 bg-danger/[0.06] px-3 py-2 text-13 text-danger-ink">Lumen can&rsquo;t ask this safely: {error}</p>
  );
}

// ---------------------------------------------------------------------------------------------------

export function PendingQuestionCard({
  question,
  onSelect,
  onFinalAction,
  onUploadArtifact,
  onGenerateArtifact,
  onUpdateArtifact,
  onApplyArtifact,
  sending
}: {
  question: OperatorQuestion;
  onSelect: SelectFn;
  onFinalAction: FinalActionFn;
  onUploadArtifact: UploadArtifactFn;
  onGenerateArtifact: GenerateArtifactFn;
  onUpdateArtifact: UpdateArtifactFn;
  onApplyArtifact: ApplyArtifactFn;
  sending: boolean;
}) {
  if (question.question_type === "final_action_approval") {
    return <FinalActionApproval question={question} contract={finalActionContract(question)} sending={sending} onFinalAction={onFinalAction} />;
  }

  const fields = fieldPrompts(question);
  const singleField = fields.length === 1 ? fields[0] : null;
  const documentField = singleField && isDocumentField(singleField) ? singleField : null;
  const singleFieldIsChoice = Boolean(singleField && isChoiceField(singleField));
  const singleFieldOptions = singleField ? fieldOptions(singleField) : [];
  const genericOptions = !singleField ? schemaOptions(question) : [];
  // Confirm-ask: a single field carrying the agent's best guess renders approve / edit / write.
  const singleChoiceContractReady = Boolean(singleField && (!singleFieldIsChoice || confirmationChoiceContractReady(singleField, singleFieldOptions.length)));
  const confirmField = !documentField && singleField && proposedConfirmationValue(singleField) && singleChoiceContractReady ? singleField : null;
  // A single choice field without a proposal renders as option rows.
  const choiceField = !documentField && !confirmField && fields.length === 1 && singleFieldIsChoice ? fields[0] : null;
  const choiceOptions = choiceField ? fieldOptions(choiceField) : [];
  const invalidChoiceContract = Boolean(choiceField && !confirmationChoiceContractReady(choiceField, choiceOptions.length));
  const multi = shouldRenderFieldSummaryList(fields.length);

  return (
    <CardShell
      eyebrow={confirmField ? "Check Lumen's answer" : multi ? "Lumen needs a few answers" : "Lumen needs your answer"}
      icon={confirmField ? <LumenMark className="!size-4" /> : <MessageSquareReply />}
      badge={singleField ? <RequirementBadge field={singleField} /> : null}
    >
      {multi ? (
        <>
          <h3 className="text-base font-semibold text-fg">{question.prompt || "A few fields need you"}</h3>
          <ul className="mt-3 divide-y divide-line overflow-hidden rounded-xl border border-line">
            {fields.map((field, index) => {
              const options = fieldOptions(field);
              const context = storedContext(field);
              return (
                <li key={`${fieldLabel(field)}-${index}`} className="px-3.5 py-2.5">
                  <div className="flex items-center gap-2">
                    <p className="text-sm font-medium text-fg">{fieldLabel(field)}</p>
                    {requiredBadge(field) === "required" ? <span className="text-2xs text-attention-ink">Required</span> : null}
                  </div>
                  {options.length ? (
                    <p className="mt-0.5 text-xs text-fg-muted">
                      {isMultipleChoiceField(field) ? "Any of:" : "One of:"} {options.join(" · ")}
                    </p>
                  ) : null}
                  {context ? <p className="mt-0.5 text-xs text-fg-subtle">{context}</p> : null}
                </li>
              );
            })}
          </ul>
          <p className="mt-3 flex items-center gap-1.5 text-13 text-fg-muted">
            <MessageSquareReply className="size-3.5" /> Reply below with your answers. One message is fine.
          </p>
        </>
      ) : (
        <>
          <QuestionHeading question={question} field={singleField} hideReason={Boolean(confirmField)} />
          {documentField ? (
            <DocumentArtifactControls
              question={question}
              field={documentField}
              sending={sending}
              onUploadArtifact={onUploadArtifact}
              onGenerateArtifact={onGenerateArtifact}
              onUpdateArtifact={onUpdateArtifact}
              onApplyArtifact={onApplyArtifact}
            />
          ) : confirmField ? (
            <ProposedAnswer field={confirmField} sending={sending} onSelect={onSelect} />
          ) : choiceField && invalidChoiceContract ? (
            <InvalidQuestionContract field={choiceField} />
          ) : choiceField && isMultipleChoiceField(choiceField) ? (
            <MultiOptionList
              options={choiceOptions}
              label={fieldLabel(choiceField)}
              disabled={sending}
              onSubmit={(answer) => onSelect(answer.content, { selected_options: answer.selected_options, task_ids: taskIds(choiceField) })}
              defer={{
                ...deferFor(fieldRequired(choiceField), choiceOptions[choiceOptions.length - 1]),
                onPick: () => onSelect("Skip this field", { skip: true, task_ids: taskIds(choiceField) })
              }}
            />
          ) : choiceField ? (
            <OptionList
              options={choiceOptions}
              label={fieldLabel(choiceField)}
              disabled={sending}
              onPick={(option) => onSelect(option, { selected_option: option, task_ids: taskIds(choiceField) })}
              defer={{
                ...deferFor(fieldRequired(choiceField), choiceOptions[choiceOptions.length - 1]),
                onPick: () => onSelect("Skip this field", { skip: true, task_ids: taskIds(choiceField) })
              }}
            />
          ) : genericOptions.length ? (
            <OptionList options={genericOptions} label={question.prompt} disabled={sending} onPick={(option) => onSelect(option)} />
          ) : (
            <p className="mt-3 flex items-center gap-1.5 text-13 text-fg-muted">
              <MessageSquareReply className="size-3.5" /> Reply below to continue.
            </p>
          )}
        </>
      )}
    </CardShell>
  );
}
