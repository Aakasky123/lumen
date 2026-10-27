import assert from "node:assert/strict";
import test from "node:test";

import {
  employerMemberValidationError,
  employmentAttestationNeedsSave,
  employmentHistoryAttestationContract,
  formatEmployerMembers,
  parseEmployerMembers,
  selectEmploymentHistoryAttestation
} from "./employmentHistoryAttestation.ts";

function attestation(overrides = {}) {
  return {
    id: "attestation-1",
    scope: "employment_history",
    set_key: "employers",
    members: ["Tower Loan"],
    exhaustive: true,
    status: "active",
    attested_by: "user",
    source: "profile_attestation_api",
    version: 1,
    created_at: "2026-07-31T12:00:00Z",
    updated_at: "2026-07-31T12:00:00Z",
    revoked_at: null,
    ...overrides
  };
}

test("parses one exact employer per line while removing blank and duplicate entries", () => {
  assert.deepEqual(parseEmployerMembers(" Tower Loan \n\nAffirm\ntower loan\n"), ["Tower Loan", "Affirm"]);
  assert.equal(formatEmployerMembers(["Tower Loan", "Affirm"]), "Tower Loan\nAffirm");
});

test("selects the active employment employer-set attestation", () => {
  const selected = selectEmploymentHistoryAttestation([
    attestation({ id: "attestation-2", status: "revoked", revoked_at: "2026-07-31T13:00:00Z", updated_at: "2026-07-31T13:00:00Z" }),
    attestation({ id: "attestation-3", scope: "education_history", set_key: "schools" }),
    attestation({ id: "attestation-4", members: ["Affirm"], updated_at: "2026-07-31T14:00:00Z" })
  ]);

  assert.equal(selected?.id, "attestation-4");
  assert.deepEqual(selected?.members, ["Affirm"]);
  assert.deepEqual(employmentHistoryAttestationContract, { scope: "employment_history", setKey: "employers" });
});

test("falls back to the latest revoked record for review without treating it as active", () => {
  const selected = selectEmploymentHistoryAttestation([
    attestation({ id: "attestation-8", status: "revoked", updated_at: "2026-07-31T15:00:00Z", revoked_at: "2026-07-31T15:00:00Z" }),
    attestation({ id: "attestation-9", status: "revoked", updated_at: "2026-07-31T16:00:00Z", revoked_at: "2026-07-31T16:00:00Z" })
  ]);

  assert.equal(selected?.id, "attestation-9");
  assert.equal(employmentAttestationNeedsSave(selected, selected?.members || []), true);
});

test("any member edit requires a new save while an unchanged active list does not", () => {
  const active = attestation({ members: ["Tower Loan", "Affirm"] });

  assert.equal(employmentAttestationNeedsSave(active, ["Tower Loan", "Affirm"]), false);
  assert.equal(employmentAttestationNeedsSave(active, ["Tower Loan", "AFFIRM"]), true);
  assert.equal(employmentAttestationNeedsSave(active, ["Tower Loan"]), true);
  assert.equal(employmentAttestationNeedsSave(null, []), true);
});

test("mirrors the backend member-count and member-length limits", () => {
  assert.equal(employerMemberValidationError(Array.from({ length: 101 }, (_, index) => `Employer ${index}`)), "Enter no more than 100 employers.");
  assert.equal(employerMemberValidationError(["x".repeat(241)]), "Each employer name must be 240 characters or fewer.");
  assert.equal(employerMemberValidationError([]), "");
});
