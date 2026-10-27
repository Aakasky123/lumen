import type { ChatThread, OperatorRun } from "@/lib/api";

/**
 * A model spend in US dollars for display: "$0.27", "$1,204.50", or "<$0.01" for a spend that
 * rounds to nothing. Null when there is nothing to show (no run, or not a usable number).
 */
export function formatCostUsd(value: number | null | undefined): string | null {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return null;
  if (value > 0 && value < 0.01) return "<$0.01";
  const [whole, cents] = value.toFixed(2).split(".");
  return `$${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}.${cents}`;
}

/**
 * A chat's model spend. The thread list's `cost_usd` is every run the thread carried; the chat's
 * own run carries its `cost_estimate` and is often polled faster. The larger is shown: the live run
 * for a one-run thread, the thread total once earlier runs add to it. A run passed without a thread
 * (or for a thread the list has not bound yet) is the chat's run.
 */
export function threadCostUsd(
  thread: Pick<ChatThread, "run_id" | "cost_usd"> | null | undefined,
  run: Pick<OperatorRun, "id" | "cost_estimate"> | null | undefined
): number | null {
  const threadTotal = typeof thread?.cost_usd === "number" ? thread.cost_usd : null;
  if (run && (!thread || thread.run_id == null || thread.run_id === run.id) && typeof run.cost_estimate === "number") {
    return threadTotal === null ? run.cost_estimate : Math.max(threadTotal, run.cost_estimate);
  }
  return threadTotal;
}
