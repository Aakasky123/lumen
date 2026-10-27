import type { ChatMessage } from "@/lib/api";

/**
 * Drop repeated system narration from a thread: an assistant message the runtime posted (it carries
 * a payload `source`, e.g. run status) whose text matches an earlier one from the same source.
 * Conversational replies carry no source and are always kept, even when they repeat ("Hi" twice
 * gets two answers). Shared by the cockpit and the Android app.
 */
export function dedupeChatMessages(items: ChatMessage[]): ChatMessage[] {
  const seen = new Set<string>();
  const result: ChatMessage[] = [];
  for (const message of items) {
    const source = String(message.payload_json?.source || "");
    if (message.role !== "assistant" || message.payload_json?.client_status || !source) {
      result.push(message);
      continue;
    }
    const text = String(message.content || "").replace(/\s+/g, " ").trim().toLowerCase();
    if (!text) {
      result.push(message);
      continue;
    }
    const key = `${source}:${String(message.payload_json?.question_type || "")}:${text}`;
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(message);
  }
  return result;
}
