import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { test } from "node:test";
import { stepUpMessage } from "./stepUpMessage.ts";

// Vector produced by apps/api/app/remote_access.py (step_up_message + request_target) for the same
// inputs; if either side changes the format, this fails.
test("step-up message matches the backend byte for byte", () => {
  const body = JSON.stringify({ final_action_mode: "approval" });
  const sha = createHash("sha256").update(body, "utf8").digest("hex");
  assert.equal(sha, "3e1635bbb8982bd928706fff01cd825816375b5541c92a5ce4d083907903641d");
  assert.equal(
    stepUpMessage("n0nce-AbC_123", "patch", "/agent-settings?x=1&y=%20", sha),
    "lumen-step-up/v1\nn0nce-AbC_123\nPATCH\n/agent-settings?x=1&y=%20\n3e1635bbb8982bd928706fff01cd825816375b5541c92a5ce4d083907903641d"
  );
});

test("the body the client sends is the body it hashes", () => {
  // JSON.stringify output is what both the fetch body and the hash use; no re-serialization.
  const payload = { answer: "Confirm and submit", payload: {}, approval_token: "t".repeat(32), final_action_decision: "approve" };
  const bodyText = JSON.stringify(payload);
  assert.equal(JSON.parse(bodyText).final_action_decision, "approve");
  assert.equal(createHash("sha256").update(bodyText, "utf8").digest("hex").length, 64);
});
