import type { ChatThread, OperatorRun } from "@/lib/api";

const DEFAULT_TITLES = new Set(["lumen chat", "lumen run chat"]);

// Same rule as utils.parseBackendDate (naive backend timestamps are UTC); kept local so this module
// stays importable by the node unit tests.
function parseDate(value: string): Date {
  const s = String(value || "").trim();
  if (s && !/(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(s)) return new Date(s.replace(" ", "T") + "Z");
  return new Date(s);
}

function oneLine(value: string): string {
  return String(value || "").split(/\r?\n/).map((line) => line.trim()).find(Boolean)?.replace(/\s+/g, " ") || "";
}

/** A link reads as noise in a title (the target host is shown beside it); drop it if words remain. */
function withoutUrls(text: string): string {
  const stripped = text
    .replace(/https?:\/\/\S+/g, " ")
    .replace(/\s+/g, " ")
    .replace(/[\s:,;–—-]+$/, "")
    .trim();
  return stripped.length >= 4 ? stripped : text;
}

/** The words a chat is listed under: the run's goal when it has one, else the thread title. */
export function threadTitle(thread: Pick<ChatThread, "title">, run?: Pick<OperatorRun, "goal"> | null): string {
  const goal = withoutUrls(oneLine(run?.goal || ""));
  if (goal) return goal;
  const title = oneLine(thread.title);
  return !title || DEFAULT_TITLES.has(title.toLowerCase()) ? "New chat" : title;
}

export type ThreadGroup<T> = { label: string; items: T[] };

/** Today / Yesterday / Previous 7 days / Previous 30 days / Older, newest first, empty groups dropped. */
export function groupByRecency<T extends Pick<ChatThread, "updated_at">>(threads: T[], now: Date = new Date()): ThreadGroup<T>[] {
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const day = 86_400_000;
  const buckets: ThreadGroup<T>[] = [
    { label: "Today", items: [] },
    { label: "Yesterday", items: [] },
    { label: "Previous 7 days", items: [] },
    { label: "Previous 30 days", items: [] },
    { label: "Older", items: [] }
  ];
  const sorted = [...threads].sort((a, b) => parseDate(b.updated_at).getTime() - parseDate(a.updated_at).getTime());
  for (const thread of sorted) {
    const time = parseDate(thread.updated_at).getTime();
    const index = !Number.isFinite(time)
      ? 4
      : time >= startOfToday
        ? 0
        : time >= startOfToday - day
          ? 1
          : time >= startOfToday - 7 * day
            ? 2
            : time >= startOfToday - 30 * day
              ? 3
              : 4;
    buckets[index].items.push(thread);
  }
  return buckets.filter((bucket) => bucket.items.length);
}
