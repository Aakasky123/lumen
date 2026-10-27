import assert from "node:assert/strict";
import { test } from "node:test";
import { dedupeChatMessages } from "./chatMessages.ts";

let nextId = 0;
const message = (role, content, payload = {}) => ({
  id: ++nextId,
  thread_id: 1,
  role,
  content,
  payload_json: payload,
  linked_event_id: null,
  linked_task_id: null,
  created_at: "2026-10-03T00:00:00"
});

test("repeated conversational replies are all kept", () => {
  const thread = [
    message("user", "Hi"),
    message("assistant", "Hi! What would you like help with?"),
    message("user", "Hi"),
    message("assistant", "Hi! What would you like help with?")
  ];
  assert.equal(dedupeChatMessages(thread).length, 4);
});

test("repeated narration from the same source is collapsed", () => {
  const thread = [
    message("assistant", "Working on boards.greenhouse.io", { source: "in_browser_status" }),
    message("assistant", "Working on  boards.greenhouse.io", { source: "in_browser_status" }),
    message("assistant", "Working on boards.greenhouse.io", { source: "in_browser_outcome" })
  ];
  assert.deepEqual(
    dedupeChatMessages(thread).map((item) => item.payload_json.source),
    ["in_browser_status", "in_browser_outcome"]
  );
});

test("client placeholders are never collapsed", () => {
  const thread = [message("assistant", "", { client_status: "loading" }), message("assistant", "", { client_status: "loading" })];
  assert.equal(dedupeChatMessages(thread).length, 2);
});
