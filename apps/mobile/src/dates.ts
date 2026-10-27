/** Backend timestamps without a zone are UTC (same rule as the cockpit's parseBackendDate). */
export function parseBackendDate(value: string | null | undefined): Date {
  const s = String(value || "").trim();
  if (s && !/(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(s)) return new Date(s.replace(" ", "T") + "Z");
  return new Date(s);
}

export function timeOfDay(value: string): string {
  const date = parseBackendDate(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}
