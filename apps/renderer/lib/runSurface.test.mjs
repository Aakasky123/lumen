import assert from "node:assert/strict";
import test from "node:test";

import { systemBlockFromRun } from "./runSurface.ts";

function runWith(state, confidence) {
  return {
    id: 509,
    current_state: state,
    status_message: "I stopped because the browser agent hit an internal/provider failure (vision_model_timeout).",
    updated_at: "2026-08-01T00:00:00Z",
    confidence_state: confidence
  };
}

const blockedConfidence = {
  system_block: { reason: "vision_model_timeout", needs_user: false, block_kind: "awaiting_engineering" },
  end_to_end: { system_block: true, system_block_reason: "vision_model_timeout", block_kind: "awaiting_engineering" }
};

test("shows the system block while the run is actually stopped", () => {
  const view = systemBlockFromRun(runWith("BLOCKED", blockedConfidence));
  assert.ok(view);
  assert.equal(view.reason, "vision_model_timeout");
  assert.equal(view.blockKind, "awaiting_engineering");
});

test("suppresses a stale system block once the run is working again", () => {
  for (const state of ["RUNNING", "FILLING_APPLICATION", "CREATED", "running"]) {
    assert.equal(systemBlockFromRun(runWith(state, blockedConfidence)), null, `state ${state} should suppress the banner`);
  }
});

test("keeps showing the block when the lifecycle state is missing", () => {
  assert.ok(systemBlockFromRun(runWith("", blockedConfidence)));
});

test("a runtime-restart block is resumable with a calm title; other blocks are not", () => {
  const restarted = systemBlockFromRun({
    id: 9,
    current_state: "BLOCKED",
    status_message: "The Lumen runtime restarted while this run was executing.",
    confidence_state: {
      system_block: true,
      system_block_reason: "runtime_restarted",
      block_kind: "runtime_restart",
      in_browser: { device_id: "dev-1" }
    }
  });
  assert.ok(restarted);
  assert.equal(restarted.resumable, true);
  assert.equal(restarted.title, "Lumen restarted during this task");

  const engineering = systemBlockFromRun({
    id: 10,
    current_state: "BLOCKED",
    status_message: "Provider failure.",
    confidence_state: {
      system_block: true,
      block_kind: "awaiting_engineering"
    }
  });
  assert.ok(engineering);
  assert.equal(engineering.resumable, false);
  assert.equal(engineering.title, "Lumen hit an internal problem");
});
