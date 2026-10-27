import assert from "node:assert/strict";
import test from "node:test";

import { PHASE_META, runContextUsage, runPhase, runTargetLabel } from "./runPhase.ts";

const run = (current_state, confidence_state = {}) => ({ current_state, confidence_state });

test("terminal and review states map to their own phases", () => {
  assert.equal(runPhase(run("COMPLETED")), "done");
  assert.equal(runPhase(run("FAILED")), "failed");
  assert.equal(runPhase(run("CANCELLED")), "ended");
  assert.equal(runPhase(run("READY_FOR_REVIEW")), "review");
  assert.equal(runPhase(run("BLOCKED")), "stopped");
  assert.equal(runPhase(null), null);
});

test("a waiting run needs you unless it is paused or waiting to resume", () => {
  assert.equal(runPhase(run("WAITING_FOR_USER")), "needs_you");
  assert.equal(runPhase(run("WAITING_FOR_USER", { user_pause: { active: true } })), "paused");
  assert.equal(runPhase(run("WAITING_FOR_USER", { session_control: { status: "PAUSED" } })), "paused");
  assert.equal(runPhase(run("WAITING_FOR_USER", { in_browser_resume_retry: { pending: true } })), "paused");
});

test("every other state is working, and only working, paused, and needs_you are live", () => {
  for (const state of ["RUNNING", "FILLING_APPLICATION", "CREATED", "running"]) {
    assert.equal(runPhase(run(state)), "working", state);
  }
  const live = Object.entries(PHASE_META).filter(([, meta]) => meta.live).map(([phase]) => phase).sort();
  assert.deepEqual(live, ["needs_you", "paused", "working"]);
});

test("target labels name the host, or Windows for the desktop surface", () => {
  assert.equal(runTargetLabel("https://www.zillow.com/fremont-ca/"), "zillow.com");
  assert.equal(runTargetLabel("desktop://windows", false), "Windows");
  assert.equal(runTargetLabel("desktop://windows", true), "Mac");
  assert.equal(runTargetLabel(""), "");
  assert.equal(runTargetLabel("not a url"), "not a url");
});

test("context usage needs a positive token count and keeps an unknown limit unknown", () => {
  assert.equal(runContextUsage(run("RUNNING", {})), null);
  assert.equal(runContextUsage(run("RUNNING", { context_usage: { used_tokens: 0 } })), null);
  assert.deepEqual(runContextUsage(run("RUNNING", { context_usage: { used_tokens: 1200, model: "m" } })), {
    usedTokens: 1200,
    limitTokens: null,
    model: "m",
    source: ""
  });
});
