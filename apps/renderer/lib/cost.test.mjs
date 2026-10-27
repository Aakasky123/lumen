import assert from "node:assert/strict";
import test from "node:test";

import { formatCostUsd, threadCostUsd } from "./cost.ts";

test("formats a spend in dollars and cents", () => {
  assert.equal(formatCostUsd(0.27), "$0.27");
  assert.equal(formatCostUsd(0.5), "$0.50");
  assert.equal(formatCostUsd(3), "$3.00");
  assert.equal(formatCostUsd(0.016), "$0.02");
  assert.equal(formatCostUsd(1204.5), "$1,204.50");
  assert.equal(formatCostUsd(0), "$0.00");
});

test("a spend that rounds to nothing still reads as a spend", () => {
  assert.equal(formatCostUsd(0.004), "<$0.01");
  assert.equal(formatCostUsd(0.0099), "<$0.01");
  assert.equal(formatCostUsd(0.01), "$0.01");
});

test("shows nothing without a usable figure", () => {
  assert.equal(formatCostUsd(null), null);
  assert.equal(formatCostUsd(undefined), null);
  assert.equal(formatCostUsd(Number.NaN), null);
  assert.equal(formatCostUsd(Number.POSITIVE_INFINITY), null);
  assert.equal(formatCostUsd(-1), null);
});

test("the thread's own run is the fresher figure", () => {
  assert.equal(threadCostUsd({ run_id: 7, cost_usd: 0.1 }, { id: 7, cost_estimate: 0.12 }), 0.12);
  assert.equal(threadCostUsd({ run_id: null, cost_usd: null }, { id: 7, cost_estimate: 0.03 }), 0.03);
  assert.equal(threadCostUsd(null, { id: 7, cost_estimate: 0.03 }), 0.03);
});

test("a thread total from earlier runs is not hidden by its live run", () => {
  assert.equal(threadCostUsd({ run_id: 7, cost_usd: 0.3 }, { id: 7, cost_estimate: 0.12 }), 0.3);
});

test("another run never stands in for the thread's spend", () => {
  assert.equal(threadCostUsd({ run_id: 7, cost_usd: 0.1 }, { id: 8, cost_estimate: 0.5 }), 0.1);
  assert.equal(threadCostUsd({ run_id: 7, cost_usd: 0.1 }, null), 0.1);
  assert.equal(threadCostUsd({ run_id: null, cost_usd: null }, null), null);
  assert.equal(threadCostUsd({ run_id: null }, undefined), null);
  assert.equal(threadCostUsd(null, null), null);
});
