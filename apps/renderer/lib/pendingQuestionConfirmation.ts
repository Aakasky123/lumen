type PendingConfirmationField = {
  proposed_answer?: unknown;
  proposed_option?: unknown;
  display_reason?: unknown;
  ask_reason?: unknown;
  evidence_coverage?: unknown;
  source_coverage?: unknown;
  source_coverage_explanation?: unknown;
  coverage_explanation?: unknown;
  confirmation_required?: unknown;
  question_contract_valid?: unknown;
  requires_fresh_observation?: unknown;
  needs_option_observation?: unknown;
  choice_question_state?: unknown;
  options_binding_status?: unknown;
};

function cleanString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function explanationFromCoverage(value: unknown): string {
  const direct = cleanString(value);
  if (direct) return direct;
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";

  const coverage = value as Record<string, unknown>;
  for (const key of [
    "explanation",
    "summary",
    "reason",
    "message",
    "coverage_summary",
    "source_coverage_explanation"
  ]) {
    const explanation = cleanString(coverage[key]);
    if (explanation) return explanation;
  }
  return "";
}

function coverageObject(field: PendingConfirmationField): Record<string, unknown> | null {
  const value = field.evidence_coverage;
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function readableScope(value: unknown): string {
  const normalized = cleanString(value).toLowerCase();
  const labels: Record<string, string> = {
    profile: "profile",
    resume: "résumé",
    saved_answer: "saved answers",
    saved_answers: "saved answers",
    human_answer: "prior answers",
    human_answers: "prior answers",
    memory: "approved memory"
  };
  return labels[normalized] || normalized.replaceAll("_", " ");
}

function listPhrase(values: string[]): string {
  if (values.length < 2) return values[0] || "";
  if (values.length === 2) return `${values[0]} and ${values[1]}`;
  return `${values.slice(0, -1).join(", ")}, and ${values[values.length - 1]}`;
}

export function proposedConfirmationValue(field: PendingConfirmationField): string {
  return cleanString(field.proposed_answer) || cleanString(field.proposed_option);
}

export function confirmationEvidenceExplanation(field: PendingConfirmationField): string {
  for (const coverage of [
    field.evidence_coverage,
    field.source_coverage,
    field.source_coverage_explanation,
    field.coverage_explanation
  ]) {
    const explanation = explanationFromCoverage(coverage);
    if (explanation) return explanation;
  }

  return cleanString(field.display_reason) || cleanString(field.ask_reason);
}

export function confirmationCoverageCaveat(field: PendingConfirmationField): string {
  const coverage = coverageObject(field);
  if (!coverage) return "";

  const scopes = Array.isArray(coverage.searched_scopes)
    ? [...new Set(coverage.searched_scopes.map(readableScope).filter(Boolean))]
    : [];
  const checked = scopes.length
    ? `Checked ${listPhrase(scopes)}.`
    : "Checked the authorized candidate context.";
  const hasResultCount = coverage.result_count !== null && coverage.result_count !== undefined;
  const noMatch = coverage.matching_fact_found === false || (hasResultCount && Number(coverage.result_count) === 0);
  const finding = noMatch ? " No matching fact was found in those sources." : "";
  const bounded = coverage.bounded === true || coverage.exhaustive === false;
  const limitation = bounded
    ? " This was a bounded, non-exhaustive check—not proof of absence—so Lumen will not apply the proposal without your confirmation."
    : " The proposal still requires your confirmation before Lumen applies it.";

  return `${checked}${finding}${limitation}`;
}

export function confirmationIsRequired(field: PendingConfirmationField): boolean {
  return field.confirmation_required !== false;
}

export function confirmationChoiceContractReady(field: PendingConfirmationField, optionCount: number): boolean {
  if (optionCount <= 0 || field.question_contract_valid === false) return false;
  if (field.requires_fresh_observation === true || field.needs_option_observation === true) return false;
  if (cleanString(field.choice_question_state).toLowerCase() === "reobserve_or_hydrate_required") return false;
  if (cleanString(field.options_binding_status).toLowerCase() === "mismatched_reobserve_required") return false;
  return true;
}

export function shouldRenderFieldSummaryList(fieldCount: number): boolean {
  return Number.isFinite(fieldCount) && fieldCount > 1;
}

/** A field the backend marks `multiple: true` takes one or more of its options (a checkbox group). */
export function isMultipleChoiceField(field: { multiple?: unknown }): boolean {
  return field.multiple === true;
}

/**
 * The chat message for a multi-select answer: the picked options in the options' displayed order,
 * de-duplicated, joined by ", ". Null when nothing valid is picked.
 */
export function multiSelectAnswer(options: string[], selected: Iterable<string>): { content: string; selected_options: string[] } | null {
  const picked = new Set(selected);
  const ordered = [...new Set(options)].filter((option) => picked.has(option));
  return ordered.length ? { content: ordered.join(", "), selected_options: ordered } : null;
}

/**
 * The options a multi-select proposal names (display order), or [] when any part of the proposal is
 * not one of the options. A proposal is the options joined by ", " (or a single option).
 */
export function proposedMultiSelection(options: string[], proposed: string): string[] {
  const text = cleanString(proposed).toLowerCase();
  if (!text) return [];
  const whole = options.find((option) => option.toLowerCase() === text);
  if (whole) return [whole];
  const normalize = (value: string) => value.toLowerCase().replace(/\s*,\s*/g, ", ");
  let remaining = `, ${normalize(text)}, `;
  const matched = new Set<string>();
  // Longest options first so an option containing a comma is consumed before its fragments.
  for (const option of [...options].sort((a, b) => b.length - a.length)) {
    const token = `, ${normalize(option)}, `;
    if (remaining.includes(token)) {
      remaining = remaining.replace(token, ", ");
      matched.add(option);
    }
  }
  if (remaining.replace(/[\s,]+/g, "")) return [];
  return options.filter((option) => matched.has(option));
}
