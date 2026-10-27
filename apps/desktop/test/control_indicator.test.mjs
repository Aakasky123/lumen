import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { controlIndicatorDataUrl, controlIndicatorHtml } from "../control_indicator.mjs";

const TEST_DIR = path.dirname(fileURLToPath(import.meta.url));
const MAIN_SOURCE = fs.readFileSync(path.join(TEST_DIR, "..", "main.mjs"), "utf8");

test("desktop indicator is a breathing activity pill with a live status hook", () => {
  const html = controlIndicatorHtml();

  assert.match(html, /Lumen is using your computer/);
  // The edge glow is the LUMEN mint, with a dark inner hairline so it reads over light apps too.
  assert.match(html, /border: 3px solid rgba\(52, 214, 168/);
  assert.match(html, /inset 0 0 0 1px rgba\(4, 22, 16/);
  assert.match(html, /pointer-events: none/);
  assert.match(html, /@keyframes lumen-pulse/);
  assert.match(html, /@keyframes lumen-breathe/);
  assert.match(html, /prefers-reduced-motion/);
  assert.match(html, /__lumenControlStatus/);
  // The live status line renders as inert text only.
  assert.match(html, /textContent/);
  assert.match(controlIndicatorDataUrl(), /^data:text\/html;charset=utf-8,/);
});

test("pill shows the keep-awake tag only while the run holds the machine awake", () => {
  const html = controlIndicatorHtml();

  assert.match(html, /<span id="awake" class="awake" hidden>Keeping screen on<\/span>/);
  assert.match(html, /awake\.hidden = !keepAwake/);
  assert.match(MAIN_SOURCE, /forwardControlStatus\(status\?\.control_indicator\?\.message, status\?\.keep_awake\)/);
});

test("desktop host creates protected click-through overlays and polls exact body activity", () => {
  assert.match(MAIN_SOURCE, /setIgnoreMouseEvents\(true/);
  assert.match(MAIN_SOURCE, /setContentProtection\(true\)/);
  assert.match(MAIN_SOURCE, /setAlwaysOnTop\(true, "screen-saver"\)/);
  assert.match(MAIN_SOURCE, /operator\/desktop\/status/);
  assert.match(MAIN_SOURCE, /control_indicator\?\.active/);
  // Every status poll refreshes the pill's live message while the overlay is up.
  assert.match(MAIN_SOURCE, /forwardControlStatus/);
  assert.match(MAIN_SOURCE, /__lumenControlStatus/);
});

test("one failed status poll does not end the control indicator or restore the window", () => {
  assert.match(MAIN_SOURCE, /const STATUS_MISSES_BEFORE_INACTIVE = 3;/);
  assert.match(MAIN_SOURCE, /if \(!status\) \{\s*consecutiveStatusMisses \+= 1;\s*if \(consecutiveStatusMisses < STATUS_MISSES_BEFORE_INACTIVE\) return;/);
});
