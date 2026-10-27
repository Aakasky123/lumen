import type { ClosedWorldAttestation } from "@/lib/api";

const EMPLOYMENT_SCOPE = "employment_history";
const EMPLOYER_SET_KEY = "employers";

export const employmentHistoryAttestationContract = {
  scope: EMPLOYMENT_SCOPE,
  setKey: EMPLOYER_SET_KEY
} as const;

export function parseEmployerMembers(value: string): string[] {
  const members: string[] = [];
  const seen = new Set<string>();
  for (const line of value.split(/\r?\n/)) {
    const member = line.trim();
    const key = member.toLowerCase();
    if (!member || seen.has(key)) continue;
    seen.add(key);
    members.push(member);
  }
  return members;
}

export function formatEmployerMembers(members: readonly string[]): string {
  return members.map((member) => String(member || "").trim()).filter(Boolean).join("\n");
}

function employerMembersEqual(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((member, index) => member === right[index]);
}

export function employerMemberValidationError(members: readonly string[]): string {
  if (members.length > 100) return "Enter no more than 100 employers.";
  if (members.some((member) => member.length > 240)) return "Each employer name must be 240 characters or fewer.";
  return "";
}

function isEmploymentHistoryAttestation(item: ClosedWorldAttestation): boolean {
  return item.scope === EMPLOYMENT_SCOPE && item.set_key === EMPLOYER_SET_KEY;
}

export function selectEmploymentHistoryAttestation(items: readonly ClosedWorldAttestation[]): ClosedWorldAttestation | null {
  const matching = items.filter(isEmploymentHistoryAttestation);
  const active = matching.filter((item) => item.status === "active");
  const candidates = active.length ? active : matching;
  return (
    [...candidates].sort((left, right) => {
      const timeDifference = Date.parse(right.updated_at) - Date.parse(left.updated_at);
      return Number.isFinite(timeDifference) && timeDifference !== 0 ? timeDifference : right.id.localeCompare(left.id);
    })[0] || null
  );
}

export function employmentAttestationNeedsSave(attestation: ClosedWorldAttestation | null, members: readonly string[]): boolean {
  return !attestation || attestation.status !== "active" || !employerMembersEqual(attestation.members, members);
}
