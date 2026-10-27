import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { displayForPoint, pointerOverlayDataUrl, pointerOverlayHtml } from "../pointer_overlay.mjs";

const TEST_DIR = path.dirname(fileURLToPath(import.meta.url));
const MAIN_SOURCE = fs.readFileSync(path.join(TEST_DIR, "..", "main.mjs"), "utf8");
const PACKAGE = JSON.parse(fs.readFileSync(path.join(TEST_DIR, "..", "package.json"), "utf8"));

test("desktop pointer overlay is a click-through animated cursor surface", () => {
  const html = pointerOverlayHtml();

  assert.match(html, /pointer-events: none/);
  assert.match(html, /__lumenDesktopPointer/);
  assert.match(html, /@keyframes lumen-ripple/);
  // Idle parks the cursor visibly at its last position instead of hiding it.
  assert.match(html, /parked/);
  assert.match(html, /@keyframes lumen-park-pulse/);
  assert.match(html, /prefers-reduced-motion/);
  assert.match(html, /typing…/);
  assert.match(pointerOverlayDataUrl(), /^data:text\/html;charset=utf-8,/);
});

test("pointer events route to the display that contains them, with a primary fallback", () => {
  const primary = { bounds: { x: 0, y: 0, width: 1920, height: 1080 } };
  const left = { bounds: { x: -1280, y: 120, width: 1280, height: 720 } };
  const entries = [primary, left];

  assert.equal(displayForPoint(entries, { x: 100, y: 200 }), primary);
  assert.equal(displayForPoint(entries, { x: -640, y: 400 }), left);
  assert.equal(
    displayForPoint(entries, { x: 99999, y: 99999 }),
    primary,
    "a point outside every display still renders on the primary overlay",
  );
  assert.equal(displayForPoint(entries, {}), primary);
  assert.equal(displayForPoint([], { x: 1, y: 1 }), null);
});

test("desktop host feeds body pointer gestures into protected overlay windows", () => {
  // Pointer windows share the protected click-through window factory with the indicator,
  // poll the exact body feed, convert physical pixels to DIPs, and never replay stale events.
  assert.match(MAIN_SOURCE, /createProtectedOverlayWindow/);
  assert.match(MAIN_SOURCE, /operator\/desktop\/pointer\?since=/);
  assert.match(MAIN_SOURCE, /screenToDipPoint/);
  assert.match(MAIN_SOURCE, /__lumenDesktopPointer/);
  assert.match(MAIN_SOURCE, /pointerFeedPrimed/);
  assert.ok(
    PACKAGE.build.files.includes("pointer_overlay.mjs"),
    "the packaged app must bundle the pointer overlay module",
  );
});
