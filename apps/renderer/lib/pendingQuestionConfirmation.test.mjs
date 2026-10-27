import assert from "node:assert/strict";
import test from "node:test";

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
} from "./pendingQuestionConfirmation.ts";

test("reads an explicit evidence coverage explanation for a pending proposal", () => {
  const field = {
    evidence_coverage: {
      summary: "The provided profile and documents contain no employment entry for this company."
    },
    display_reason: "Fallback reason",
    proposed_answer: "No",
    confirmation_required: true
  };

  assert.equal(
    confirmationEvidenceExplanation(field),
    "The provided profile and documents contain no employment entry for this company."
  );
  assert.equal(proposedConfirmationValue(field), "No");
  assert.equal(confirmationIsRequired(field), true);
});

test("preserves the existing display reason and proposed option contract", () => {
  const field = {
    display_reason: "Lumen found partial support and needs you to confirm the choice.",
    proposed_option: "No"
  };

  assert.equal(
    confirmationEvidenceExplanation(field),
    "Lumen found partial support and needs you to confirm the choice."
  );
  assert.equal(proposedConfirmationValue(field), "No");
  assert.equal(confirmationIsRequired(field), true);
});

test("supports source coverage aliases and an explicit non-required marker", () => {
  const field = {
    source_coverage_explanation: "Only the approved profile was checked.",
    proposed_answer: "Review manually",
    confirmation_required: false
  };

  assert.equal(confirmationEvidenceExplanation(field), "Only the approved profile was checked.");
  assert.equal(confirmationIsRequired(field), false);
});

test("summarizes bounded zero-result coverage without exposing query or receipt internals", () => {
  const field = {
    display_reason: "Your provided documents do not list employment at this company.",
    proposed_answer: "No",
    confirmation_required: true,
    evidence_coverage: {
      query: "private internal retrieval query",
      requested_scope: "candidate_context",
      searched_scopes: ["profile", "resume", "saved_answers", "human_answers"],
      candidate_count: 12,
      result_count: 0,
      bounded: true,
      exhaustive: false,
      matching_fact_found: false,
      receipt_sha256: "secret-internal-receipt"
    }
  };

  const caveat = confirmationCoverageCaveat(field);
  assert.match(caveat, /Checked profile, résumé, saved answers, and prior answers\./);
  assert.match(caveat, /No matching fact was found in those sources\./);
  assert.match(caveat, /bounded, non-exhaustive check—not proof of absence/);
  assert.doesNotMatch(caveat, /private internal retrieval query/);
  assert.doesNotMatch(caveat, /secret-internal-receipt/);
});

test("does not offer proposal approval against missing or stale live choice bindings", () => {
  assert.equal(confirmationChoiceContractReady({}, 2), true);
  assert.equal(confirmationChoiceContractReady({}, 0), false);
  assert.equal(confirmationChoiceContractReady({ question_contract_valid: false }, 2), false);
  assert.equal(confirmationChoiceContractReady({ requires_fresh_observation: true }, 2), false);
  assert.equal(confirmationChoiceContractReady({ needs_option_observation: true }, 2), false);
  assert.equal(confirmationChoiceContractReady({ choice_question_state: "reobserve_or_hydrate_required" }, 2), false);
  assert.equal(confirmationChoiceContractReady({ options_binding_status: "mismatched_reobserve_required" }, 2), false);
});

test("renders a field summary list only when a pending question contains multiple fields", () => {
  assert.equal(shouldRenderFieldSummaryList(0), false);
  assert.equal(shouldRenderFieldSummaryList(1), false);
  assert.equal(shouldRenderFieldSummaryList(2), true);
});

test("only an explicit multiple: true makes a field multi-select", () => {
  assert.equal(isMultipleChoiceField({ multiple: true }), true);
  assert.equal(isMultipleChoiceField({ multiple: false }), false);
  assert.equal(isMultipleChoiceField({ multiple: "true" }), false);
  assert.equal(isMultipleChoiceField({}), false);
});

test("a multi-select answer keeps the options' display order and joins with a comma", () => {
  const options = ["Compensation", "Remote work", "Growth", "Team", "Mission"];
  assert.deepEqual(multiSelectAnswer(options, ["Mission", "Compensation", "Growth"]), {
    content: "Compensation, Growth, Mission",
    selected_options: ["Compensation", "Growth", "Mission"]
  });
  assert.deepEqual(multiSelectAnswer(options, ["Team", "Team", "Not an option"]), { content: "Team", selected_options: ["Team"] });
  assert.equal(multiSelectAnswer(options, []), null);
  assert.equal(multiSelectAnswer(options, ["Not an option"]), null);
});

test("a multi-select proposal is approvable only when every part is a real option", () => {
  const options = ["Compensation", "Growth", "Team", "Salary, bonus"];
  assert.deepEqual(proposedMultiSelection(options, "team,  compensation"), ["Compensation", "Team"]);
  assert.deepEqual(proposedMultiSelection(options, "Growth"), ["Growth"]);
  assert.deepEqual(proposedMultiSelection(options, "Salary, bonus, Team"), ["Team", "Salary, bonus"]);
  assert.deepEqual(proposedMultiSelection(options, "Growth, Free lunch"), []);
  assert.deepEqual(proposedMultiSelection(options, ""), []);
});
