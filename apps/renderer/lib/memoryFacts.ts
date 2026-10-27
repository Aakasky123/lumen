/**
 * What Lumen knows about the user (the profile), as the backend returns it from GET /memory, plus
 * how both the cockpit and the Android app describe where a fact came from. Framework-free so the
 * phone imports it too.
 */
export type MemorySection = { key: string; title: string };

export type MemoryFact = {
  key: string;
  label: string;
  value: string;
  section: string;
  /** YYYY-MM-DD the fact was last written. */
  learned_on: string | null;
  /** Free text the backend stamps, e.g. "run 12, you, in chat" or "from Resume.pdf". */
  source: string | null;
};

export type MemoryPayload = { sections: MemorySection[]; facts: MemoryFact[] };

const SOURCE_PHRASES: Array<[RegExp, string]> = [
  [/^you, in chat$/i, "You told Lumen"],
  [/^you, answering a question$/i, "Your answer during a task"],
  [/^your correction$/i, "Your correction"],
  [/^added by you$/i, "Added by you"],
  [/^edited by you$/i, "Edited by you"]
];

/** "You told Lumen", "From Resume.pdf", "Edited by you"... or "" when the source is unknown. */
export function describeFactSource(source: string | null | undefined): string {
  const text = String(source || "")
    .replace(/^run \d+,?\s*/i, "")
    .trim();
  if (!text) return "";
  for (const [pattern, phrase] of SOURCE_PHRASES) {
    if (pattern.test(text)) return phrase;
  }
  if (/^from /i.test(text)) return `From ${text.slice(5).trim()}`;
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** Facts Lumen learned on its own in the last two days (not ones the user typed in the profile). */
export function factIsNew(fact: Pick<MemoryFact, "learned_on" | "source">, now: Date = new Date()): boolean {
  if (!fact.learned_on) return false;
  const phrase = describeFactSource(fact.source);
  if (phrase === "Added by you" || phrase === "Edited by you") return false;
  const learned = new Date(`${fact.learned_on}T00:00:00Z`).getTime();
  return Number.isFinite(learned) && now.getTime() - learned < 2 * 86_400_000;
}

/** "Oct 3" for a YYYY-MM-DD date. */
export function shortFactDate(day: string | null | undefined): string {
  if (!day) return "";
  const date = new Date(`${day}T12:00:00Z`);
  return Number.isFinite(date.getTime()) ? date.toLocaleDateString([], { month: "short", day: "numeric" }) : "";
}

/** Sections in display order with their facts; empty sections are dropped. */
export function groupFacts(payload: MemoryPayload): Array<MemorySection & { facts: MemoryFact[] }> {
  return payload.sections
    .map((section) => ({ ...section, facts: payload.facts.filter((fact) => fact.section === section.key) }))
    .filter((section) => section.facts.length > 0);
}
