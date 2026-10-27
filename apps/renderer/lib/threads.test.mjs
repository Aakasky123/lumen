import assert from "node:assert/strict";
import test from "node:test";

import { groupByRecency, threadTitle } from "./threads.ts";

test("a run's goal names its chat; default titles read as a new chat", () => {
  assert.equal(threadTitle({ title: "Browser task: zillow.com" }, { goal: "Find 2-bedroom apartments\nunder $3,200" }), "Find 2-bedroom apartments");
  assert.equal(threadTitle({ title: "Browser task: zillow.com" }, { goal: "  " }), "Browser task: zillow.com");
  assert.equal(threadTitle({ title: "Lumen chat" }), "New chat");
  assert.equal(threadTitle({ title: "Lumen run chat" }, null), "New chat");
  assert.equal(threadTitle({ title: "What can you do?" }), "What can you do?");
});

test("links drop out of a goal title unless the link is all there is", () => {
  assert.equal(
    threadTitle({ title: "x" }, { goal: "Apply to the Backend Engineer role at Stripe: https://boards.greenhouse.io/stripe/jobs/1" }),
    "Apply to the Backend Engineer role at Stripe"
  );
  assert.equal(threadTitle({ title: "x" }, { goal: "https://example.com/form" }), "https://example.com/form");
});

test("threads group by recency, newest first, and empty groups are dropped", () => {
  const now = new Date(2026, 9, 2, 15, 0, 0);
  const at = (days, hours = 0) => new Date(now.getTime() - days * 86_400_000 - hours * 3_600_000).toISOString();
  const threads = [
    { id: 1, updated_at: at(0, 1) },
    { id: 2, updated_at: at(1) },
    { id: 3, updated_at: at(3) },
    { id: 4, updated_at: at(0, 3) },
    { id: 5, updated_at: at(60) }
  ];
  const groups = groupByRecency(threads, now);
  assert.deepEqual(groups.map((group) => group.label), ["Today", "Yesterday", "Previous 7 days", "Older"]);
  assert.deepEqual(groups[0].items.map((item) => item.id), [1, 4]);
});

test("naive backend timestamps are read as UTC", () => {
  const now = new Date(Date.UTC(2026, 9, 2, 12, 0, 0));
  const groups = groupByRecency([{ id: 1, updated_at: "2026-10-02 11:30:00.123456" }], now);
  assert.equal(groups.length, 1);
});
