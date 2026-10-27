import * as DocumentPicker from "expo-document-picker";
import {
  ArrowRight,
  Bookmark,
  Check,
  CreditCard,
  FileText,
  FileUp,
  Info,
  MessageSquareReply,
  PenLine,
  ShieldCheck
} from "lucide-react-native";
import { useState } from "react";
import { Pressable, View } from "react-native";
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
import { api, type ApplicationArtifact, type OperatorQuestion } from "@/api";
import { errorMessage } from "@/hooks";
import {
  acceptedExtensions,
  artifactOptions,
  answerModes,
  cleanString,
  deferFor,
  documentKind,
  fieldLabel,
  fieldOptions,
  fieldPrompts,
  fieldRequired,
  finalActionContract,
  humanKind,
  isChoiceField,
  isDocumentField,
  requiredBadge,
  schemaOptions,
  schemaString,
  storedContext,
  taskIds,
  type FieldPrompt,
  type FinalActionContract
} from "@/questions";
import { LumenMark } from "@/components/LumenMark";
import { Badge, Banner, Button, CardShell, Field, Spinner, T } from "@/components/ui";
import { alpha, radius, useTheme } from "@/theme";

export type SelectFn = (text: string, extra?: Record<string, unknown>) => void;
export type FinalActionFn = (question: OperatorQuestion, contract: FinalActionContract, choice: { label: string; decision: "approve" | "decline" }) => void;

function QuestionHeading({ question, field, hideReason }: { question: OperatorQuestion; field: FieldPrompt | null; hideReason?: boolean }) {
  const { c } = useTheme();
  const title = (field && (schemaString(question, "display_title") || cleanString(field.display_title || field.observed_label || field.label))) || question.prompt;
  const subtitle = field ? schemaString(question, "display_subtitle") || cleanString(field.display_subtitle) : "";
  const reason = field ? schemaString(question, "display_reason") || cleanString(field.display_reason || field.ask_reason) : "";
  const context = title !== question.prompt ? cleanString(question.prompt) : "";
  return (
    <View style={{ gap: 8 }}>
      <T variant="title">{title || "Lumen has a question"}</T>
      {subtitle ? (
        <View style={{ flexDirection: "row" }}>
          <Badge label={subtitle} />
        </View>
      ) : null}
      {context ? <T variant="small">{context}</T> : null}
      {!hideReason && reason && reason !== context ? (
        <View style={{ flexDirection: "row", gap: 8 }}>
          <Info size={14} color={c.fgSubtle} style={{ marginTop: 2 }} />
          <T variant="small" style={{ flex: 1 }}>
            {reason}
          </T>
        </View>
      ) : null}
    </View>
  );
}

function OptionList({
  options,
  disabled,
  onPick,
  defer
}: {
  options: string[];
  disabled: boolean;
  onPick: (option: string) => void;
  defer?: { label: string; hint: string; onPick: () => void };
}) {
  const { c } = useTheme();
  return (
    <View style={{ gap: 8 }}>
      <View style={{ borderRadius: radius.lg, borderWidth: 1, borderColor: c.line, overflow: "hidden" }}>
        {options.map((option, index) => (
          <Pressable
            key={`${option}-${index}`}
            accessibilityRole="button"
            disabled={disabled}
            onPress={() => onPick(option)}
            style={({ pressed }) => ({
              flexDirection: "row",
              alignItems: "center",
              gap: 12,
              minHeight: 50,
              paddingHorizontal: 14,
              paddingVertical: 10,
              borderTopWidth: index ? 1 : 0,
              borderTopColor: c.line,
              backgroundColor: pressed ? alpha(c.brand, 0.08) : "transparent",
              opacity: disabled ? 0.5 : 1
            })}
          >
            <View style={{ width: 18, height: 18, borderRadius: 9, borderWidth: 1.5, borderColor: c.lineStrong }} />
            <T style={{ flex: 1 }}>{option}</T>
            <ArrowRight size={15} color={c.fgSubtle} />
          </Pressable>
        ))}
        {defer ? (
          <Pressable
            accessibilityRole="button"
            disabled={disabled}
            onPress={defer.onPick}
            style={({ pressed }) => ({
              flexDirection: "row",
              alignItems: "center",
              gap: 12,
              minHeight: 50,
              paddingHorizontal: 14,
              borderTopWidth: options.length ? 1 : 0,
              borderTopColor: c.line,
              backgroundColor: pressed ? alpha(c.fg, 0.06) : alpha(c.fg, 0.025)
            })}
          >
            <MessageSquareReply size={16} color={c.fgMuted} />
            <T color={c.fgMuted}>{defer.label}</T>
          </Pressable>
        ) : null}
      </View>
      {defer ? <T variant="caption">{defer.hint}</T> : null}
    </View>
  );
}

type MultiSelectAnswer = { content: string; selected_options: string[] };

// A checkbox group: rows toggle, nothing is sent until the user taps Continue.
function MultiOptionList({
  options,
  disabled,
  initial,
  onSubmit,
  defer
}: {
  options: string[];
  disabled: boolean;
  initial?: string[];
  onSubmit: (answer: MultiSelectAnswer) => void;
  defer?: { label: string; hint: string; onPick: () => void };
}) {
  const { c } = useTheme();
  const [selected, setSelected] = useState<string[]>(() => (initial || []).filter((option) => options.includes(option)));
  const toggle = (option: string) => setSelected((prev) => (prev.includes(option) ? prev.filter((item) => item !== option) : [...prev, option]));
  const answer = multiSelectAnswer(options, selected);
  const count = answer?.selected_options.length ?? 0;
  return (
    <View style={{ gap: 8 }}>
      <View style={{ borderRadius: radius.lg, borderWidth: 1, borderColor: c.line, overflow: "hidden" }}>
        {options.map((option, index) => {
          const checked = selected.includes(option);
          return (
            <Pressable
              key={`${option}-${index}`}
              accessibilityRole="checkbox"
              accessibilityState={{ checked, disabled }}
              accessibilityLabel={option}
              disabled={disabled}
              onPress={() => toggle(option)}
              style={({ pressed }) => ({
                flexDirection: "row",
                alignItems: "center",
                gap: 12,
                minHeight: 50,
                paddingHorizontal: 14,
                paddingVertical: 10,
                borderTopWidth: index ? 1 : 0,
                borderTopColor: c.line,
                backgroundColor: pressed ? alpha(c.brand, 0.08) : checked ? alpha(c.brand, 0.05) : "transparent",
                opacity: disabled ? 0.5 : 1
              })}
            >
              <View
                style={{
                  width: 18,
                  height: 18,
                  borderRadius: 5,
                  borderWidth: 1.5,
                  borderColor: checked ? c.brand : c.lineStrong,
                  backgroundColor: checked ? c.brand : "transparent",
                  alignItems: "center",
                  justifyContent: "center"
                }}
              >
                {checked ? <Check size={12} strokeWidth={3} color={c.brandFg} /> : null}
              </View>
              <T style={{ flex: 1 }}>{option}</T>
            </Pressable>
          );
        })}
        {defer ? (
          <Pressable
            accessibilityRole="button"
            disabled={disabled}
            onPress={defer.onPick}
            style={({ pressed }) => ({
              flexDirection: "row",
              alignItems: "center",
              gap: 12,
              minHeight: 50,
              paddingHorizontal: 14,
              borderTopWidth: options.length ? 1 : 0,
              borderTopColor: c.line,
              backgroundColor: pressed ? alpha(c.fg, 0.06) : alpha(c.fg, 0.025)
            })}
          >
            <MessageSquareReply size={16} color={c.fgMuted} />
            <T color={c.fgMuted}>{defer.label}</T>
          </Pressable>
        ) : null}
      </View>
      <Button
        label={count ? `Continue with ${count}` : "Continue"}
        icon={Check}
        variant="primary"
        disabled={disabled || !answer}
        onPress={() => answer && onSubmit(answer)}
        style={{ alignSelf: "flex-start" }}
      />
      <T variant="caption">{count ? "Nothing is sent until you continue." : "Pick one or more."}</T>
      {defer ? <T variant="caption">{defer.hint}</T> : null}
    </View>
  );
}

function EvidenceNote({ field }: { field: FieldPrompt }) {
  const { c } = useTheme();
  const explanation = confirmationEvidenceExplanation(field);
  const caveat = confirmationCoverageCaveat(field);
  const [open, setOpen] = useState(false);
  if (!explanation && !caveat) return null;
  return (
    <View style={{ gap: 4 }}>
      {explanation ? (
        <View style={{ flexDirection: "row", gap: 8 }}>
          <Info size={14} color={c.fgSubtle} style={{ marginTop: 2 }} />
          <T variant="small" style={{ flex: 1 }}>
            {explanation}
          </T>
        </View>
      ) : null}
      {caveat ? (
        <>
          <Pressable onPress={() => setOpen((value) => !value)} style={{ marginLeft: 22 }} hitSlop={8}>
            <T variant="caption" weight="600">
              {open ? "Hide how Lumen checked" : "How Lumen checked"}
            </T>
          </Pressable>
          {open ? (
            <T variant="caption" style={{ marginLeft: 22 }}>
              {caveat}
            </T>
          ) : null}
        </>
      ) : null}
    </View>
  );
}

function Suggestion({ value, approvalRequired }: { value: string; approvalRequired: boolean }) {
  const { c } = useTheme();
  return (
    <View style={{ borderRadius: radius.lg, borderWidth: 1, borderColor: alpha(c.brand, 0.25), backgroundColor: alpha(c.brand, 0.05), padding: 12, gap: 6 }}>
      <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
        <LumenMark size={14} />
        <T variant="caption" weight="600" color={c.brandInk}>
          Lumen suggests
        </T>
        <T variant="caption" style={{ marginLeft: "auto" }}>
          {approvalRequired ? "Not on the page until you approve" : "Not on the page yet"}
        </T>
      </View>
      <T selectable>{value}</T>
    </View>
  );
}

// Confirm-ask: the proposal stays pending until the user explicitly approves or supplies an answer.
function ProposedAnswer({ field, sending, onSelect }: { field: FieldPrompt; sending: boolean; onSelect: SelectFn }) {
  const proposed = proposedConfirmationValue(field);
  const options = fieldOptions(field);
  const ids = taskIds(field);
  const [editMode, setEditMode] = useState<"idle" | "edit" | "reject">("idle");
  const [draft, setDraft] = useState("");
  const approvalRequired = confirmationIsRequired(field);
  const required = fieldRequired(field);
  const keep = () => onSelect(proposed, { keep_proposed: true, task_ids: ids });
  // A multi-select proposal ("A, B") must name only real options.
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
          <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
            <Button
              label={proposedSelection.length > 1 ? `Use these ${proposedSelection.length}` : `Use “${matchedOption}”`}
              icon={Check}
              variant="primary"
              disabled={sending}
              onPress={keep}
            />
            <Button label={multiple ? "Change the picks" : "Choose another"} variant="ghost" disabled={sending} onPress={() => setEditMode("edit")} />
          </View>
        ) : null}
        {!matchedOption ? (
          <Banner tone="attention">
            {multiple ? "That suggestion doesn't match the form's options, so pick them yourself." : "That suggestion isn't one of the form's options, so pick one yourself."}
          </Banner>
        ) : null}
        {editMode !== "idle" || !matchedOption ? (
          <>
            {multiple ? (
              <MultiOptionList
                options={options}
                disabled={sending}
                initial={proposedSelection}
                onSubmit={(answer) => onSelect(answer.content, { selected_options: answer.selected_options, task_ids: ids })}
                defer={{ ...defer, onPick: () => onSelect("Skip this field", { skip: true, task_ids: ids }) }}
              />
            ) : (
              <OptionList
                options={options}
                disabled={sending}
                onPick={(option) => onSelect(option, { selected_option: option, task_ids: ids })}
                defer={{ ...defer, onPick: () => onSelect("Skip this field", { skip: true, task_ids: ids }) }}
              />
            )}
            {matchedOption ? <Button label="Back to the suggestion" variant="ghost" size="sm" disabled={sending} onPress={() => setEditMode("idle")} style={{ alignSelf: "flex-start" }} /> : null}
          </>
        ) : null}
      </>
    );
  }

  // Free text: approve, edit, and write-your-own are distinct. Edit and write only open a local draft.
  return (
    <>
      {editMode === "idle" ? <Suggestion value={proposed} approvalRequired={approvalRequired} /> : null}
      <EvidenceNote field={field} />
      {editMode !== "idle" ? (
        <View style={{ gap: 10 }}>
          <Field
            autoFocus
            multiline
            value={draft}
            onChangeText={setDraft}
            placeholder={editMode === "reject" ? "Write the answer Lumen should use…" : undefined}
            accessibilityLabel={editMode === "edit" ? "Edit the suggested answer" : "Your answer"}
          />
          <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
            <Button label="Use this answer" icon={Check} variant="primary" disabled={sending || !draft.trim()} onPress={() => onSelect(draft.trim(), { task_ids: ids })} />
            <Button
              label="Cancel"
              variant="ghost"
              disabled={sending}
              onPress={() => {
                setDraft("");
                setEditMode("idle");
              }}
            />
          </View>
          <T variant="caption">Nothing is sent until you choose Use this answer.</T>
        </View>
      ) : (
        <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
          <Button label="Use this answer" icon={Check} variant="primary" disabled={sending} onPress={keep} />
          <Button
            label="Use & remember"
            icon={Bookmark}
            disabled={sending}
            onPress={() => onSelect(proposed, { keep_proposed: true, task_ids: ids, save_scope: "permanent" })}
          />
          <Button
            label="Edit"
            icon={PenLine}
            variant="ghost"
            disabled={sending}
            onPress={() => {
              setDraft(proposed);
              setEditMode("edit");
            }}
          />
          <Button
            label="Write my own"
            variant="ghost"
            disabled={sending}
            onPress={() => {
              setDraft("");
              setEditMode("reject");
            }}
          />
        </View>
      )}
    </>
  );
}

function DocumentControls({ question, field, sending, onChanged }: { question: OperatorQuestion; field: FieldPrompt; sending: boolean; onChanged: () => void }) {
  const { c } = useTheme();
  const modes = answerModes(field);
  const options = artifactOptions(field);
  const canUpload = modes.includes("upload_file");
  const canGenerate = modes.includes("generate_artifact");
  const canChoose = modes.includes("choose_artifact") && options.length > 0;
  const tabs = [
    ...(canChoose ? [{ key: "saved" as const, label: `Saved (${options.length})` }] : []),
    ...(canUpload ? [{ key: "upload" as const, label: "Upload" }] : []),
    ...(canGenerate ? [{ key: "generate" as const, label: "Write with Lumen" }] : [])
  ];
  const [tab, setTab] = useState(tabs[0]?.key ?? "upload");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [draft, setDraft] = useState<ApplicationArtifact | null>(null);
  const [draftText, setDraftText] = useState("");
  const [instructions, setInstructions] = useState("");
  const kind = humanKind(documentKind(field));
  const accepted = acceptedExtensions(field);

  const run = async (operation: () => Promise<void>, success?: string) => {
    setBusy(true);
    setStatus(null);
    try {
      await operation();
      if (success) setStatus({ tone: "ok", text: success });
      onChanged();
    } catch (error) {
      const message = errorMessage(error);
      if (message) setStatus({ tone: "error", text: message });
    } finally {
      setBusy(false);
    }
  };

  const pickAndUpload = async () => {
    const picked = await DocumentPicker.getDocumentAsync({ copyToCacheDirectory: true, multiple: false });
    if (picked.canceled || !picked.assets?.[0]) return;
    const asset = picked.assets[0];
    const extension = asset.name.split(".").pop()?.toLowerCase() ?? "";
    if (!accepted.includes(extension)) {
      setStatus({ tone: "error", text: `This field takes ${accepted.map((item) => `.${item}`).join(", ")} files. ${asset.name} isn't one.` });
      return;
    }
    await run(async () => {
      await api.uploadQuestionArtifact(question.id, { uri: asset.uri, name: asset.name, mimeType: asset.mimeType });
    }, `Uploaded ${asset.name}. Lumen checks the live field when it resumes.`);
  };

  const generate = () =>
    run(async () => {
      const result = await api.generateQuestionArtifact(question.id, { instructions: instructions.trim() || undefined, artifact_kind: documentKind(field) });
      if (result.artifact) {
        setDraft(result.artifact);
        setDraftText(result.artifact.text_content || result.artifact.text_preview || "");
      }
    });

  const applyDraft = () =>
    run(async () => {
      if (!draft) return;
      let artifact = draft;
      if (draftText.trim() && draftText !== (draft.text_content || "")) {
        artifact = await api.updateArtifact(draft.id, draftText.trim());
        setDraft(artifact);
      }
      await api.applyQuestionArtifact(question.id, artifact.id);
    }, "Saved for this form. Lumen checks the live field when it resumes.");

  const disabled = sending || busy;
  return (
    <View style={{ gap: 12 }}>
      {tabs.length > 1 ? (
        <View style={{ flexDirection: "row", padding: 3, borderRadius: radius.md, borderWidth: 1, borderColor: c.line, backgroundColor: alpha(c.fg, 0.04), alignSelf: "flex-start" }}>
          {tabs.map((item) => (
            <Pressable
              key={item.key}
              accessibilityRole="tab"
              accessibilityState={{ selected: tab === item.key }}
              onPress={() => setTab(item.key)}
              style={{ paddingHorizontal: 12, paddingVertical: 6, borderRadius: radius.sm, backgroundColor: tab === item.key ? c.surface1 : "transparent" }}
            >
              <T variant="small" weight="600" color={tab === item.key ? c.fg : c.fgMuted}>
                {item.label}
              </T>
            </Pressable>
          ))}
        </View>
      ) : null}

      {tab === "saved" && canChoose ? (
        <View style={{ gap: 8 }}>
          {options.map((artifact) => (
            <Pressable
              key={artifact.id}
              disabled={disabled}
              onPress={() => run(() => api.applyQuestionArtifact(question.id, artifact.id).then(() => undefined), "Saved for this form. Lumen checks the live field when it resumes.")}
              style={({ pressed }) => ({
                flexDirection: "row",
                gap: 12,
                padding: 12,
                borderRadius: radius.lg,
                borderWidth: 1,
                borderColor: pressed ? alpha(c.brand, 0.4) : c.line,
                backgroundColor: pressed ? alpha(c.brand, 0.05) : c.surface1,
                opacity: disabled ? 0.5 : 1
              })}
            >
              <FileText size={18} color={c.fgMuted} />
              <View style={{ flex: 1, gap: 2 }}>
                <T weight="500" numberOfLines={1}>
                  {artifact.name}
                </T>
                {artifact.preview ? (
                  <T variant="caption" numberOfLines={2}>
                    {artifact.preview}
                  </T>
                ) : null}
              </View>
            </Pressable>
          ))}
        </View>
      ) : null}

      {tab === "upload" && canUpload ? (
        <Pressable
          accessibilityRole="button"
          disabled={disabled}
          onPress={() => void pickAndUpload()}
          style={({ pressed }) => ({
            alignItems: "center",
            gap: 6,
            paddingVertical: 22,
            borderRadius: radius.lg,
            borderWidth: 1,
            borderStyle: "dashed",
            borderColor: pressed ? c.brand : c.lineStrong,
            opacity: disabled ? 0.5 : 1
          })}
        >
          {busy ? <Spinner /> : <FileUp size={20} color={c.fgMuted} />}
          <T weight="500">{busy ? "Uploading…" : `Choose a ${kind} from this phone`}</T>
          <T variant="caption">{accepted.map((item) => `.${item}`).join(" ")}</T>
        </Pressable>
      ) : null}

      {tab === "generate" && canGenerate ? (
        draft ? (
          <View style={{ gap: 10 }}>
            <T variant="caption" weight="500">
              Draft — edit anything before you use it
            </T>
            <Field multiline value={draftText} onChangeText={setDraftText} style={{ minHeight: 200 }} accessibilityLabel="Draft document" />
            <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
              <Button label="Use this draft" icon={Check} variant="primary" loading={busy} disabled={disabled || !draftText.trim()} onPress={() => void applyDraft()} />
              <Button label="Write another" variant="ghost" disabled={disabled} onPress={() => void generate()} />
            </View>
          </View>
        ) : (
          <View style={{ gap: 10 }}>
            <Field value={instructions} onChangeText={setInstructions} placeholder="Anything to emphasize? (optional)" accessibilityLabel="Notes for the draft" />
            <Button label={busy ? "Writing…" : `Write a ${kind}`} icon={PenLine} variant="primary" loading={busy} disabled={disabled} onPress={() => void generate()} style={{ alignSelf: "flex-start" }} />
            <T variant="caption">Lumen drafts from your profile and résumé. You review it before anything reaches the form.</T>
          </View>
        )
      ) : null}

      {status ? <T variant="small" color={status.tone === "error" ? c.dangerInk : c.brandInk}>{status.text}</T> : null}
    </View>
  );
}

function FinalActionCard({ question, sending, onFinalAction }: { question: OperatorQuestion; sending: boolean; onFinalAction: FinalActionFn }) {
  const { c } = useTheme();
  const contract = finalActionContract(question);
  if (!contract) {
    return (
      <CardShell eyebrow="Approve the final step" icon={ShieldCheck}>
        <T variant="small">{question.prompt}</T>
        <Banner tone="danger">This approval is out of date or incomplete, so Lumen won't take the final step. It will ask again if it can.</Banner>
      </CardShell>
    );
  }
  return (
    <CardShell
      eyebrow={contract.paymentFloor ? "Confirm the payment" : "Approve the final step"}
      icon={contract.paymentFloor ? CreditCard : ShieldCheck}
      badge={contract.paymentFloor ? <Badge label="Involves payment" tone="attention" /> : null}
    >
      {contract.submitLabel ? (
        <View style={{ gap: 2 }}>
          <T variant="eyebrow">Lumen will tap</T>
          <T variant="heading">“{contract.submitLabel}”</T>
        </View>
      ) : null}
      {contract.summary ? (
        <Banner tone="attention" icon={CreditCard}>
          <T weight="500">{contract.summary}</T>
        </Banner>
      ) : null}
      <T variant="small">{question.prompt}</T>
      <View style={{ gap: 8 }}>
        {contract.choices.map((choice) => (
          <Button
            key={`${choice.decision}-${choice.label}`}
            label={choice.label}
            icon={choice.decision === "approve" ? Check : undefined}
            size="lg"
            variant={choice.decision === "approve" ? (contract.paymentFloor ? "attention" : "primary") : "secondary"}
            disabled={sending}
            onPress={() => onFinalAction(question, contract, choice)}
          />
        ))}
      </View>
      <T variant="caption">
        {contract.paymentFloor ? "You'll confirm with your fingerprint. " : ""}This approval works once, for that exact button. If the page changes first, Lumen asks again.
      </T>
    </CardShell>
  );
}

function InvalidContract({ field }: { field: FieldPrompt }) {
  const { c } = useTheme();
  const refreshing = Boolean(
    field.requires_fresh_observation ||
      field.needs_option_observation ||
      cleanString(field.choice_question_state) === "reobserve_or_hydrate_required" ||
      cleanString(field.options_binding_status) === "mismatched_reobserve_required" ||
      !fieldOptions(field).length
  );
  return refreshing ? (
    <View style={{ flexDirection: "row", gap: 10, alignItems: "center" }}>
      <Spinner />
      <T variant="small">Re-reading the field's options from the page before asking.</T>
    </View>
  ) : (
    <Banner tone="danger">{`Lumen can't ask this safely: ${cleanString(field.question_contract_error) || "The field label or option snapshot is missing."}`}</Banner>
  );
}

export function QuestionCard({
  question,
  sending,
  onSelect,
  onFinalAction,
  onChanged
}: {
  question: OperatorQuestion;
  sending: boolean;
  onSelect: SelectFn;
  onFinalAction: FinalActionFn;
  onChanged: () => void;
}) {
  const { c } = useTheme();
  if (question.question_type === "final_action_approval") {
    return <FinalActionCard question={question} sending={sending} onFinalAction={onFinalAction} />;
  }
  const fields = fieldPrompts(question);
  const singleField = fields.length === 1 ? fields[0] : null;
  const documentField = singleField && isDocumentField(singleField) ? singleField : null;
  const singleIsChoice = Boolean(singleField && isChoiceField(singleField));
  const singleOptions = singleField ? fieldOptions(singleField) : [];
  const genericOptions = !singleField ? schemaOptions(question) : [];
  const choiceReady = Boolean(singleField && (!singleIsChoice || confirmationChoiceContractReady(singleField, singleOptions.length)));
  const confirmField = !documentField && singleField && proposedConfirmationValue(singleField) && choiceReady ? singleField : null;
  const choiceField = !documentField && !confirmField && singleField && singleIsChoice ? singleField : null;
  const choiceOptions = choiceField ? fieldOptions(choiceField) : [];
  const invalidChoice = Boolean(choiceField && !confirmationChoiceContractReady(choiceField, choiceOptions.length));
  const multi = shouldRenderFieldSummaryList(fields.length);
  const badge = singleField ? (
    <Badge label={{ required: "Required", optional: "Optional", voluntary: "Voluntary" }[requiredBadge(singleField)]} tone={requiredBadge(singleField) === "required" ? "attention" : "neutral"} />
  ) : null;

  return (
    <CardShell
      eyebrow={confirmField ? "Check Lumen's answer" : multi ? "Lumen needs a few answers" : "Lumen needs your answer"}
      icon={MessageSquareReply}
      badge={badge}
    >
      {multi ? (
        <>
          <T variant="title">{question.prompt || "A few fields need you"}</T>
          <View style={{ borderRadius: radius.lg, borderWidth: 1, borderColor: c.line }}>
            {fields.map((field, index) => {
              const options = fieldOptions(field);
              const context = storedContext(field);
              return (
                <View key={`${fieldLabel(field)}-${index}`} style={{ padding: 12, gap: 2, borderTopWidth: index ? 1 : 0, borderTopColor: c.line }}>
                  <View style={{ flexDirection: "row", gap: 8, alignItems: "center" }}>
                    <T weight="500">{fieldLabel(field)}</T>
                    {requiredBadge(field) === "required" ? (
                      <T variant="caption" color={c.attentionInk}>
                        Required
                      </T>
                    ) : null}
                  </View>
                  {options.length ? (
                    <T variant="caption">
                      {isMultipleChoiceField(field) ? "Any of:" : "One of:"} {options.join(" · ")}
                    </T>
                  ) : null}
                  {context ? <T variant="caption">{context}</T> : null}
                </View>
              );
            })}
          </View>
          <T variant="small">Reply below with your answers. One message is fine.</T>
        </>
      ) : (
        <>
          <QuestionHeading question={question} field={singleField} hideReason={Boolean(confirmField)} />
          {documentField ? (
            <DocumentControls question={question} field={documentField} sending={sending} onChanged={onChanged} />
          ) : confirmField ? (
            <ProposedAnswer field={confirmField} sending={sending} onSelect={onSelect} />
          ) : choiceField && invalidChoice ? (
            <InvalidContract field={choiceField} />
          ) : choiceField && isMultipleChoiceField(choiceField) ? (
            <MultiOptionList
              options={choiceOptions}
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
              disabled={sending}
              onPick={(option) => onSelect(option, { selected_option: option, task_ids: taskIds(choiceField) })}
              defer={{
                ...deferFor(fieldRequired(choiceField), choiceOptions[choiceOptions.length - 1]),
                onPick: () => onSelect("Skip this field", { skip: true, task_ids: taskIds(choiceField) })
              }}
            />
          ) : genericOptions.length ? (
            <OptionList options={genericOptions} disabled={sending} onPick={(option) => onSelect(option)} />
          ) : (
            <T variant="small">Reply below to continue.</T>
          )}
        </>
      )}
    </CardShell>
  );
}
